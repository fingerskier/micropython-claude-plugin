"""File operations for MicroPython devices.

All ops wrap their work in ``device.raw_repl_session()`` so a batch of
operations (e.g. sync_directory) shares one raw REPL enter/exit rather
than paying that cost per call.

File writes stream via raw-paste mode in ~4 KB chunks and verify with a
sha256 compare at the end, which is robust over flaky USB links.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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


class FileOperations:
    """File operations for MicroPython devices."""

    # Raw bytes per chunk when writing. 4 KiB is comfortable on any
    # MicroPython target with >=16 KiB free RAM, and batches enough data
    # to keep raw-paste flow control saturated.
    WRITE_CHUNK_SIZE = 4096

    # Bytes per READ chunk when streaming off device. Smaller because
    # the *output path* (stdout of raw REPL) is line-buffered on some
    # ports and large chunks can stall.
    READ_CHUNK_SIZE = 1024

    def __init__(self, device: MicroPythonDevice):
        self.device = device

    # ------------------------------------------------------------------
    # Listing / metadata
    # ------------------------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        path = _sanitize_path(path)
        # JSON output avoids ambiguity around filenames containing '|' or
        # other delimiter chars, and gives us an easy parse failure mode.
        code = f'''
import os, json
_out = []
try:
    for name in os.listdir("{path}"):
        full = "{path}" + ("/" if "{path}" != "/" else "") + name
        try:
            st = os.stat(full)
            _out.append([name, st[6], 1 if st[0] & 0x4000 else 0, st[8] if len(st) > 8 else 0])
        except Exception:
            _out.append([name, 0, 0, 0])
    print(json.dumps(_out))
except Exception as e:
    print("ERROR:" + str(e))
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code)
        output = output.strip()
        if output.startswith("ERROR:"):
            raise RuntimeError(output[6:])
        try:
            entries = json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse list_files output: {e}; raw={output!r}"
            ) from e
        return [
            FileInfo(
                name=name,
                size=int(size),
                is_dir=bool(is_dir),
                mtime=int(mtime) if mtime else None,
            )
            for name, size, is_dir, mtime in entries
        ]

    def file_exists(self, path: str) -> bool:
        path = _sanitize_path(path)
        code = f'''
import os
try:
    os.stat("{path}")
    print("EXISTS")
except Exception:
    print("NOT_FOUND")
'''
        with self.device.raw_repl_session():
            return "EXISTS" in self.device.execute(code)

    def get_file_info(self, path: str) -> FileInfo | None:
        path = _sanitize_path(path)
        code = f'''
import os, json
try:
    st = os.stat("{path}")
    print(json.dumps([st[6], 1 if st[0] & 0x4000 else 0, st[8] if len(st) > 8 else 0]))
except Exception:
    print("NOT_FOUND")
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code).strip()
        if output == "NOT_FOUND":
            return None
        try:
            size, is_dir, mtime = json.loads(output)
        except json.JSONDecodeError:
            return None
        # Derive a sensible name; for "/" (no basename) just return "/".
        name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
        return FileInfo(
            name=name,
            size=int(size),
            is_dir=bool(is_dir),
            mtime=int(mtime) if mtime else None,
        )

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        path = _sanitize_path(path)
        code = f'''
import ubinascii
try:
    with open("{path}", "rb") as f:
        while True:
            chunk = f.read({self.READ_CHUNK_SIZE})
            if not chunk:
                break
            print(ubinascii.b2a_base64(chunk).decode().strip())
    print("EOF")
except Exception as e:
    print("ERROR:" + str(e))
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code, timeout=60.0)

        data = bytearray()
        for line in output.strip().split('\n'):
            line = line.strip()
            if line == 'EOF':
                break
            if line.startswith('ERROR:'):
                raise RuntimeError(line[6:])
            if line:
                try:
                    data.extend(base64.b64decode(line))
                except Exception:
                    pass
        return bytes(data)

    def write_file(self, path: str, content: bytes, verify: bool = True) -> None:
        """Write bytes to the device. Keeps raw REPL open and keeps a file
        handle alive across chunks via a module-level variable on the device.
        """
        path = _sanitize_path(path)

        # Ensure parent directory exists.
        parent = path.rsplit('/', 1)[0]
        if parent and parent != '/':
            self.mkdir(parent, exist_ok=True)

        host_hash = hashlib.sha256(content).hexdigest()

        with self.device.raw_repl_session():
            # Open (truncate) the device-side file handle and stash it
            # in a global so successive chunks can append without the
            # open/close dance.
            self.device.execute(
                f'_mcp_f = open("{path}", "wb")\n'
                '_mcp_h = __import__("hashlib").sha256()\n'
            )
            try:
                for i in range(0, len(content), self.WRITE_CHUNK_SIZE):
                    chunk = content[i:i + self.WRITE_CHUNK_SIZE]
                    b64 = base64.b64encode(chunk).decode('ascii')
                    code = (
                        'import ubinascii\n'
                        f'_d = ubinascii.a2b_base64(b"{b64}")\n'
                        '_mcp_f.write(_d)\n'
                        '_mcp_h.update(_d)\n'
                    )
                    # Tight per-chunk timeout scales with chunk size at
                    # 115200 baud: ~90 ms per KiB raw, doubled for base64
                    # overhead + safety margin.
                    self.device.execute(code, timeout=max(5.0, len(chunk) / 2000))
            finally:
                # Close and fetch the device-side hash.
                out = self.device.execute(
                    '_mcp_f.close()\n'
                    'import ubinascii\n'
                    'print(ubinascii.hexlify(_mcp_h.digest()).decode())\n'
                    'del _mcp_f, _mcp_h, _d\n'
                ).strip()

        if verify:
            device_hash = out.splitlines()[-1].strip() if out else ""
            if device_hash != host_hash:
                raise RuntimeError(
                    f"Post-write hash mismatch for {path}: "
                    f"host={host_hash} device={device_hash}"
                )

    def delete_file(self, path: str) -> None:
        path = _sanitize_path(path)
        code = f'''
import os
try:
    os.remove("{path}")
    print("OK")
except Exception as e:
    print("ERROR:" + str(e))
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code)
        if 'ERROR:' in output:
            raise RuntimeError(output.split('ERROR:', 1)[1].strip())

    def mkdir(self, path: str, exist_ok: bool = False) -> None:
        path = _sanitize_path(path)
        code = f'''
import os
def _mk(p):
    cur = ""
    for part in p.strip("/").split("/"):
        if not part:
            continue
        cur = cur + "/" + part
        try:
            os.mkdir(cur)
        except OSError as e:
            if e.args[0] != 17:  # EEXIST
                raise
try:
    _mk("{path}")
    print("OK")
except Exception as e:
    print("ERROR:" + str(e))
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code)
        if 'ERROR:' in output and not exist_ok:
            raise RuntimeError(output.split('ERROR:', 1)[1].strip())

    def rmdir(self, path: str, recursive: bool = False) -> None:
        path = _sanitize_path(path)
        if recursive:
            code = f'''
import os
def _rm(p):
    for entry in os.listdir(p):
        full = p + "/" + entry
        if os.stat(full)[0] & 0x4000:
            _rm(full)
        else:
            os.remove(full)
    os.rmdir(p)
try:
    _rm("{path}")
    print("OK")
except Exception as e:
    print("ERROR:" + str(e))
'''
        else:
            code = f'''
import os
try:
    os.rmdir("{path}")
    print("OK")
except Exception as e:
    print("ERROR:" + str(e))
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code, timeout=60.0)
        if 'ERROR:' in output:
            raise RuntimeError(output.split('ERROR:', 1)[1].strip())

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
                    # Under NEWEST we may have already synced this file in
                    # the upload pass; skip if local exists.
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
