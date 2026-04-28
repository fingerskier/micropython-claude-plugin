"""File operations for MicroPython devices.

Thin adapter over ``mpremote.transport_serial.SerialTransport``'s ``fs_*``
methods. All ops wrap their work in ``device.raw_repl_session()`` so a
batch of operations (e.g. sync_directory) shares one raw REPL enter/exit
rather than paying that cost per call.

Path arguments pass through ``_sanitize_path`` at every public entry —
mpremote interpolates paths into Python source via ``'%s' % path`` with
no escaping, so a stray quote would inject. Sanitization is the
single boundary that prevents that.
"""

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from mpremote.transport import TransportError

from .serial_connection import MicroPythonDevice


def _sanitize_path(path: str) -> str:
    """Reject characters that would break a Python string literal or line-oriented REPL framing."""
    forbidden = set('"\'\\;\n\r\x00')
    bad_chars = forbidden & set(path)
    if bad_chars:
        raise ValueError(f"Path contains forbidden characters: {bad_chars!r}")
    while '//' in path:
        path = path.replace('//', '/')
    return path


@dataclass
class FileInfo:
    name: str
    size: int
    is_dir: bool
    mtime: int | None = None


class SyncDirection(Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    NEWEST = "newest"


# Chunk size for fs_writefile / fs_readfile / fs_hashfile. mpremote's
# default is 256 (one exec roundtrip per chunk — slow). 1 KiB strikes a
# balance: comfortable on any MicroPython target with >= 4 KiB free RAM,
# but few enough roundtrips that a 64 KiB transfer doesn't crawl.
_CHUNK_SIZE = 1024


def _wrap_fs_error(op: str, path: str):
    """Decorator-like helper: convert mpremote's filesystem exceptions
    into the RuntimeError shape the rest of the plugin expects."""
    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb):
            if exc is None:
                return False
            if isinstance(exc, FileNotFoundError):
                return False  # let it propagate as-is
            if isinstance(exc, OSError):
                raise RuntimeError(f"{op} {path}: {exc}") from exc
            if isinstance(exc, TransportError):
                raise RuntimeError(f"{op} {path}: {exc}") from exc
            return False
    return _Ctx()


class FileOperations:
    """File operations for MicroPython devices."""

    def __init__(self, device: MicroPythonDevice):
        self.device = device

    def _transport(self):
        return self.device.transport

    # ------------------------------------------------------------------
    # Listing / metadata
    # ------------------------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        path = _sanitize_path(path)
        with self.device.raw_repl_session(), _wrap_fs_error("list_files", path):
            entries = self._transport().fs_listdir(path)
        # entries are namedtuples (name, st_mode, st_ino, st_size).
        # mtime isn't carried by os.ilistdir(); leave it None here. Per-
        # entry fs_stat would add N round-trips; callers that need mtime
        # should use get_file_info on a specific path.
        return [
            FileInfo(
                name=e.name,
                size=int(e.st_size),
                is_dir=bool(e.st_mode & 0x4000),
                mtime=None,
            )
            for e in entries
        ]

    def file_exists(self, path: str) -> bool:
        path = _sanitize_path(path)
        with self.device.raw_repl_session(), _wrap_fs_error("file_exists", path):
            return self._transport().fs_exists(path)

    def get_file_info(self, path: str) -> FileInfo | None:
        path = _sanitize_path(path)
        with self.device.raw_repl_session():
            try:
                st = self._transport().fs_stat(path)
            except OSError:
                return None
            except TransportError as e:
                raise RuntimeError(f"get_file_info {path}: {e}") from e
        name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
        # MicroPython's stat tuple uses st[8] for mtime; os.stat_result
        # exposes that as .st_mtime (may be 0 if the device doesn't track
        # mtimes, e.g. on littlefs without RTC).
        mtime = int(st.st_mtime) if st.st_mtime else None
        return FileInfo(
            name=name,
            size=int(st.st_size),
            is_dir=bool(st.st_mode & 0x4000),
            mtime=mtime,
        )

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        path = _sanitize_path(path)
        with self.device.raw_repl_session(), _wrap_fs_error("read_file", path):
            data = self._transport().fs_readfile(path, chunk_size=_CHUNK_SIZE)
        return bytes(data)

    def write_file(self, path: str, content: bytes, verify: bool = True) -> None:
        """Write bytes to the device. Optionally verify with sha256.

        Parent directories are created with ``mkdir(exist_ok=True)`` so
        callers don't have to pre-create them.
        """
        path = _sanitize_path(path)
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError(
                f"write_file requires bytes, got {type(content).__name__}"
            )

        parent = path.rsplit('/', 1)[0]
        with self.device.raw_repl_session():
            if parent and parent != '/':
                self.mkdir(parent, exist_ok=True)
            with _wrap_fs_error("write_file", path):
                self._transport().fs_writefile(
                    path, bytes(content), chunk_size=_CHUNK_SIZE
                )
            if verify:
                host_hash = hashlib.sha256(content).digest()
                with _wrap_fs_error("write_file (verify)", path):
                    device_hash = self._transport().fs_hashfile(
                        path, "sha256", chunk_size=_CHUNK_SIZE
                    )
                if device_hash != host_hash:
                    raise RuntimeError(
                        f"Post-write hash mismatch for {path}: "
                        f"host={host_hash.hex()} device={device_hash.hex()}"
                    )

    def delete_file(self, path: str) -> None:
        path = _sanitize_path(path)
        with self.device.raw_repl_session(), _wrap_fs_error("delete_file", path):
            self._transport().fs_rmfile(path)

    def mkdir(self, path: str, exist_ok: bool = False) -> None:
        """Create directory, optionally creating parents (mkdir -p style).

        Walks the path one segment at a time so an intermediate segment
        already existing doesn't fail the whole call when ``exist_ok``.
        """
        path = _sanitize_path(path)
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return  # "/" — nothing to do
        with self.device.raw_repl_session():
            cur = ""
            transport = self._transport()
            for part in parts:
                cur = cur + "/" + part
                try:
                    transport.fs_mkdir(cur)
                except OSError as e:
                    # EEXIST (errno 17) is fine if exist_ok or if this is
                    # an intermediate segment of a deeper mkdir.
                    if e.errno == 17 and (exist_ok or cur != "/" + "/".join(parts)):
                        continue
                    if not exist_ok:
                        raise RuntimeError(f"mkdir {cur}: {e}") from e
                except TransportError as e:
                    raise RuntimeError(f"mkdir {cur}: {e}") from e

    def rmdir(self, path: str, recursive: bool = False) -> None:
        path = _sanitize_path(path)
        with self.device.raw_repl_session():
            if recursive:
                self._rmdir_recursive(path)
                return
            with _wrap_fs_error("rmdir", path):
                self._transport().fs_rmdir(path)

    def _rmdir_recursive(self, path: str) -> None:
        """Walk the tree under ``path`` and delete everything, then rmdir.

        Holds a single raw REPL session for the whole walk (caller's
        responsibility — ``rmdir`` already opened one).
        """
        transport = self._transport()
        try:
            entries = transport.fs_listdir(path)
        except TransportError as e:
            raise RuntimeError(f"rmdir {path}: {e}") from e
        for entry in entries:
            full = f"{path.rstrip('/')}/{entry.name}"
            if entry.st_mode & 0x4000:
                self._rmdir_recursive(full)
            else:
                try:
                    transport.fs_rmfile(full)
                except (OSError, TransportError) as e:
                    raise RuntimeError(f"rmdir {full}: {e}") from e
        try:
            transport.fs_rmdir(path)
        except (OSError, TransportError) as e:
            raise RuntimeError(f"rmdir {path}: {e}") from e

    # ------------------------------------------------------------------
    # Host <-> device
    # ------------------------------------------------------------------

    def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        self.write_file(remote_path, local_path.read_bytes())

    def download_file(self, remote_path: str, local_path: str | Path) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.read_file(remote_path))

    def sync_file(
        self,
        local_path: str | Path,
        remote_path: str,
        direction: SyncDirection = SyncDirection.NEWEST,
    ) -> str:
        """Sync a single file between host and device.

        ``NEWEST`` direction note: MicroPython on littlefs without an RTC
        reports ``os.stat().st_mtime == 0`` (or returns it as ``None``
        through ``get_file_info``). When the remote mtime is missing,
        the comparison falls back to ``0``, so any local file with a
        non-zero mtime wins and gets uploaded. If the local file *also*
        has a zero mtime (e.g. set via ``os.utime(p, (0, 0))``) the two
        sides compare equal and the call reports "in sync" without a
        transfer. Tests pin this contract; see
        ``tests/test_fileops_adapter.py::TestSyncFileNewestMtimeFallback``.
        """
        local_path = Path(local_path)

        with self.device.raw_repl_session():
            local_exists = local_path.exists()
            remote_info = self.get_file_info(remote_path)
            remote_exists = remote_info is not None

            if direction == SyncDirection.UPLOAD:
                if not local_exists:
                    raise FileNotFoundError(f"Local file not found: {local_path}")
                self.upload_file(local_path, remote_path)
                return f"Uploaded {local_path} to {remote_path}"

            if direction == SyncDirection.DOWNLOAD:
                if not remote_exists:
                    raise FileNotFoundError(f"Remote file not found: {remote_path}")
                self.download_file(remote_path, local_path)
                return f"Downloaded {remote_path} to {local_path}"

            # NEWEST
            if not local_exists and not remote_exists:
                raise FileNotFoundError(
                    f"Neither local ({local_path}) nor remote ({remote_path}) exists"
                )
            if not local_exists:
                self.download_file(remote_path, local_path)
                return f"Downloaded {remote_path} (local didn't exist)"
            if not remote_exists:
                self.upload_file(local_path, remote_path)
                return f"Uploaded {local_path} (remote didn't exist)"

            local_mtime = int(local_path.stat().st_mtime)
            # Fallback: device with no RTC reports mtime as 0 (→ None
            # via get_file_info). Local file with current Unix time then
            # always wins. Documented in the docstring above. If both
            # sides resolve to 0, they compare equal and we report "in
            # sync" — no transfer.
            remote_mtime = remote_info.mtime or 0  # type: ignore[union-attr]

            if local_mtime > remote_mtime:
                self.upload_file(local_path, remote_path)
                return f"Uploaded {local_path} (local is newer)"
            if remote_mtime > local_mtime:
                self.download_file(remote_path, local_path)
                return f"Downloaded {remote_path} (remote is newer)"
            return "Files are in sync (same mtime)"

    def sync_directory(
        self,
        local_dir: str | Path,
        remote_dir: str,
        direction: SyncDirection = SyncDirection.NEWEST,
        pattern: str = "*",
        delete_orphans: bool = False,
    ) -> list[str]:
        """Sync a directory.

        If ``delete_orphans`` is True:
          - UPLOAD deletes remote files that are not present locally.
          - DOWNLOAD deletes local files that are not present on device.
          - NEWEST is treated as a bidirectional mirror where files missing
            on *both* sides are obviously impossible; in practice we delete
            nothing under NEWEST to avoid ambiguity (the "newest" side has
            no opinion about missing peers). Use UPLOAD/DOWNLOAD explicitly
            for one-way mirrors.
        """
        import fnmatch

        local_dir = Path(local_dir)
        results: list[str] = []

        def _rel_local_files() -> set[str]:
            if not local_dir.exists():
                return set()
            out: set[str] = set()
            for f in local_dir.rglob(pattern):
                if f.is_file():
                    rel = f.relative_to(local_dir).as_posix()
                    out.add(rel)
            return out

        def _rel_remote_files() -> set[str]:
            out: set[str] = set()
            try:
                for full in self._list_files_recursive(remote_dir):
                    rel = full[len(remote_dir):].lstrip('/')
                    if fnmatch.fnmatch(rel, pattern) or pattern == "*":
                        out.add(rel)
            except Exception as e:
                results.append(f"Error listing remote directory: {e}")
            return out

        with self.device.raw_repl_session():
            if direction in (SyncDirection.UPLOAD, SyncDirection.NEWEST):
                if local_dir.exists():
                    for rel in sorted(_rel_local_files()):
                        lf = local_dir / rel
                        rf = f"{remote_dir}/{rel}".replace("\\", "/")
                        try:
                            results.append(self.sync_file(lf, rf, direction))
                        except Exception as e:
                            results.append(f"Error syncing {lf}: {e}")

            if direction in (SyncDirection.DOWNLOAD, SyncDirection.NEWEST):
                for rel in sorted(_rel_remote_files()):
                    rf = f"{remote_dir}/{rel}".replace("\\", "/")
                    lf = local_dir / rel
                    if direction == SyncDirection.NEWEST and lf.exists():
                        continue
                    try:
                        results.append(self.sync_file(lf, rf, direction))
                    except Exception as e:
                        results.append(f"Error syncing {rf}: {e}")

            if delete_orphans:
                local_set = _rel_local_files()
                remote_set = _rel_remote_files()

                if direction == SyncDirection.UPLOAD:
                    for rel in sorted(remote_set - local_set):
                        rf = f"{remote_dir}/{rel}".replace("\\", "/")
                        try:
                            self.delete_file(rf)
                            results.append(f"Deleted orphan (remote) {rf}")
                        except Exception as e:
                            results.append(f"Error deleting {rf}: {e}")
                elif direction == SyncDirection.DOWNLOAD:
                    for rel in sorted(local_set - remote_set):
                        lf = local_dir / rel
                        try:
                            lf.unlink()
                            results.append(f"Deleted orphan (local) {lf}")
                        except Exception as e:
                            results.append(f"Error deleting {lf}: {e}")
                else:
                    results.append(
                        "delete_orphans ignored under NEWEST direction "
                        "(use upload or download for one-way mirroring)"
                    )

        return results

    def _list_files_recursive(self, path: str) -> list[str]:
        files: list[str] = []
        try:
            for entry in self.list_files(path):
                full = f"{path}/{entry.name}".replace("//", "/")
                if entry.is_dir:
                    files.extend(self._list_files_recursive(full))
                else:
                    files.append(full)
        except Exception:
            pass
        return files
