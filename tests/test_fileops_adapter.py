"""Unit tests for FileOperations as a thin adapter over mpremote's
``SerialTransport.fs_*`` surface.

These tests run without hardware. They use a ``FakeTransport`` that
records every fs_* call and lets each test pre-program return values or
errors. The goal is to lock in the contract:

  - every public FileOperations method routes to the expected fs_* call,
  - paths are sanitized at the boundary (forbidden chars rejected, double
    slashes collapsed) before they reach the transport,
  - error mapping converts mpremote's OSError / TransportError to the
    RuntimeError shape the rest of the plugin expects (with the original
    exception chained via __cause__),
  - write_file refuses non-bytes content and verifies via fs_hashfile.

Hardware-side behavior (does the device actually create the file?) is
covered by ``test_hardware_eval.py``.
"""

import hashlib
import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mpremote.transport import TransportError

from micropython_claude_plugin.file_ops import (
    FileOperations,
    FileInfo,
    _sanitize_path,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

DirEntry = namedtuple("DirEntry", ["name", "st_mode", "st_ino", "st_size"])


class StatResult:
    """Minimal stand-in for os.stat_result; mpremote's fs_stat returns
    something with .st_mode / .st_size / .st_mtime attrs."""

    def __init__(self, st_mode, st_size, st_mtime=0):
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class FakeTransport:
    """Records every fs_* call. Tests can pre-program responses or errors."""

    def __init__(self):
        self.calls: list[tuple] = []
        # programmed responses keyed by (method, path)
        self.listdir_response: dict[str, list] = {}
        self.readfile_response: dict[str, bytes] = {}
        self.stat_response: dict[str, StatResult] = {}
        self.exists_response: dict[str, bool] = {}
        self.hashfile_response: dict[str, bytes] = {}
        # programmed exceptions keyed by (method, path)
        self.errors: dict[tuple[str, str], Exception] = {}
        # paths that should already exist for mkdir EEXIST simulation
        self.existing_dirs: set[str] = set()

    def _maybe_raise(self, method: str, path: str):
        key = (method, path)
        if key in self.errors:
            raise self.errors[key]

    # -- fs_* surface --------------------------------------------------

    def fs_listdir(self, path):
        self.calls.append(("fs_listdir", path))
        self._maybe_raise("fs_listdir", path)
        return self.listdir_response.get(path, [])

    def fs_readfile(self, path, chunk_size=None):
        self.calls.append(("fs_readfile", path, chunk_size))
        self._maybe_raise("fs_readfile", path)
        return self.readfile_response.get(path, b"")

    def fs_writefile(self, path, data, chunk_size=None):
        self.calls.append(("fs_writefile", path, data, chunk_size))
        self._maybe_raise("fs_writefile", path)

    def fs_stat(self, path):
        self.calls.append(("fs_stat", path))
        self._maybe_raise("fs_stat", path)
        if path in self.stat_response:
            return self.stat_response[path]
        # Match mpremote: missing file raises OSError(ENOENT)
        raise OSError(2, "No such file/directory")

    def fs_exists(self, path):
        self.calls.append(("fs_exists", path))
        self._maybe_raise("fs_exists", path)
        return self.exists_response.get(path, False)

    def fs_rmfile(self, path):
        self.calls.append(("fs_rmfile", path))
        self._maybe_raise("fs_rmfile", path)

    def fs_rmdir(self, path):
        self.calls.append(("fs_rmdir", path))
        self._maybe_raise("fs_rmdir", path)

    def fs_mkdir(self, path):
        self.calls.append(("fs_mkdir", path))
        self._maybe_raise("fs_mkdir", path)
        if path in self.existing_dirs:
            raise OSError(17, "EEXIST")
        self.existing_dirs.add(path)

    def fs_hashfile(self, path, algo, chunk_size=None):
        self.calls.append(("fs_hashfile", path, algo, chunk_size))
        self._maybe_raise("fs_hashfile", path)
        if path in self.hashfile_response:
            return self.hashfile_response[path]
        raise OSError(2, "No such file/directory")


class FakeDevice:
    """Stands in for MicroPythonDevice. Exposes ``transport`` and
    ``raw_repl_session`` (no-op context manager)."""

    def __init__(self, transport: FakeTransport):
        self._fake = transport

    @property
    def transport(self):
        return self._fake

    class _NullCtx:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def raw_repl_session(self):
        return self._NullCtx()


@pytest.fixture
def fake_transport():
    return FakeTransport()


@pytest.fixture
def fs(fake_transport):
    return FileOperations(FakeDevice(fake_transport))


def call_names(fake_transport: FakeTransport) -> list[str]:
    return [c[0] for c in fake_transport.calls]


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------

class TestListFiles:
    def test_routes_to_fs_listdir_with_sanitized_path(self, fs, fake_transport):
        fake_transport.listdir_response["/lib"] = [
            DirEntry(name="foo.py", st_mode=0o100644, st_ino=0, st_size=42),
            DirEntry(name="sub", st_mode=0o040755, st_ino=0, st_size=0),
        ]
        entries = fs.list_files("//lib")  # double-slash collapsed
        assert fake_transport.calls == [("fs_listdir", "/lib")]
        assert len(entries) == 2
        assert entries[0] == FileInfo(name="foo.py", size=42, is_dir=False, mtime=None)
        assert entries[1].is_dir is True

    def test_default_path_is_root(self, fs, fake_transport):
        fs.list_files()
        assert fake_transport.calls == [("fs_listdir", "/")]

    def test_rejects_forbidden_chars(self, fs, fake_transport):
        with pytest.raises(ValueError, match="forbidden"):
            fs.list_files("/foo'; rm")
        # Sanitization happens before any transport call.
        assert fake_transport.calls == []

    def test_oserror_wrapped_as_runtimeerror(self, fs, fake_transport):
        # EIO (errno 5) — not auto-promoted to FileNotFoundError, so the
        # wrapper actually maps it. Distinct from the FileNotFoundError
        # passthrough case below.
        fake_transport.errors[("fs_listdir", "/bad")] = OSError(5, "EIO")
        with pytest.raises(RuntimeError, match="list_files /bad"):
            fs.list_files("/bad")

    def test_filenotfounderror_passes_through(self, fs, fake_transport):
        # Sentinel: FileNotFoundError is the one OSError subclass we let
        # callers see directly (image_ops relies on this distinction).
        fake_transport.errors[("fs_listdir", "/missing")] = FileNotFoundError(
            2, "ENOENT"
        )
        with pytest.raises(FileNotFoundError):
            fs.list_files("/missing")


# ---------------------------------------------------------------------------
# file_exists / get_file_info
# ---------------------------------------------------------------------------

class TestExistsAndStat:
    def test_file_exists_true(self, fs, fake_transport):
        fake_transport.exists_response["/main.py"] = True
        assert fs.file_exists("/main.py") is True
        assert fake_transport.calls == [("fs_exists", "/main.py")]

    def test_file_exists_false(self, fs, fake_transport):
        assert fs.file_exists("/nope") is False

    def test_get_file_info_returns_FileInfo(self, fs, fake_transport):
        fake_transport.stat_response["/lib/x.py"] = StatResult(0o100644, 123, 1700000000)
        info = fs.get_file_info("/lib/x.py")
        assert info == FileInfo(name="x.py", size=123, is_dir=False, mtime=1700000000)

    def test_get_file_info_root_name(self, fs, fake_transport):
        fake_transport.stat_response["/"] = StatResult(0o040755, 0, 0)
        info = fs.get_file_info("/")
        assert info is not None
        assert info.name == "/"
        assert info.is_dir is True
        assert info.mtime is None  # 0 mtime → None

    def test_get_file_info_returns_None_on_missing(self, fs, fake_transport):
        # default fs_stat raises OSError(ENOENT) when not pre-programmed
        assert fs.get_file_info("/ghost") is None


# ---------------------------------------------------------------------------
# read_file / write_file / delete_file
# ---------------------------------------------------------------------------

class TestReadWriteDelete:
    def test_read_file_routes_to_fs_readfile(self, fs, fake_transport):
        fake_transport.readfile_response["/data.bin"] = b"\x00\x01\x02"
        got = fs.read_file("/data.bin")
        assert got == b"\x00\x01\x02"
        assert fake_transport.calls[0][:2] == ("fs_readfile", "/data.bin")

    def test_write_file_skips_verify_when_disabled(self, fs, fake_transport):
        fs.write_file("/foo.txt", b"hello", verify=False)
        names = call_names(fake_transport)
        assert "fs_writefile" in names
        assert "fs_hashfile" not in names

    def test_write_file_verifies_via_sha256(self, fs, fake_transport):
        payload = b"hello world"
        fake_transport.hashfile_response["/foo.txt"] = hashlib.sha256(payload).digest()
        fs.write_file("/foo.txt", payload, verify=True)
        names = call_names(fake_transport)
        assert names[-2:] == ["fs_writefile", "fs_hashfile"]

    def test_write_file_raises_on_hash_mismatch(self, fs, fake_transport):
        fake_transport.hashfile_response["/foo.txt"] = b"\x00" * 32  # wrong
        with pytest.raises(RuntimeError, match="hash mismatch"):
            fs.write_file("/foo.txt", b"hello", verify=True)

    def test_write_file_rejects_str(self, fs):
        with pytest.raises(TypeError, match="requires bytes"):
            fs.write_file("/foo.txt", "not bytes", verify=False)

    def test_write_file_creates_parent_dirs(self, fs, fake_transport):
        # mkdir(exist_ok=True) walks segments; we just check fs_mkdir was
        # called for each segment of the parent before the write.
        fake_transport.hashfile_response["/a/b/c/file.txt"] = hashlib.sha256(b"x").digest()
        fs.write_file("/a/b/c/file.txt", b"x", verify=True)
        mkdir_paths = [c[1] for c in fake_transport.calls if c[0] == "fs_mkdir"]
        assert mkdir_paths == ["/a", "/a/b", "/a/b/c"]

    def test_write_file_no_parent_when_root_level(self, fs, fake_transport):
        fake_transport.hashfile_response["/foo.txt"] = hashlib.sha256(b"x").digest()
        fs.write_file("/foo.txt", b"x", verify=True)
        assert "fs_mkdir" not in call_names(fake_transport)

    def test_delete_file_routes_to_fs_rmfile(self, fs, fake_transport):
        fs.delete_file("/foo.txt")
        assert fake_transport.calls == [("fs_rmfile", "/foo.txt")]

    def test_write_path_sanitized(self, fs, fake_transport):
        with pytest.raises(ValueError):
            fs.write_file("/foo;rm", b"x")
        with pytest.raises(ValueError):
            fs.read_file("/foo'bad")
        with pytest.raises(ValueError):
            fs.delete_file("/foo\nbad")


# ---------------------------------------------------------------------------
# mkdir
# ---------------------------------------------------------------------------

class TestMkdir:
    def test_single_segment(self, fs, fake_transport):
        fs.mkdir("/foo")
        assert fake_transport.calls == [("fs_mkdir", "/foo")]

    def test_walks_segments_for_nested(self, fs, fake_transport):
        fs.mkdir("/a/b/c")
        mkdir_paths = [c[1] for c in fake_transport.calls if c[0] == "fs_mkdir"]
        assert mkdir_paths == ["/a", "/a/b", "/a/b/c"]

    def test_exist_ok_swallows_eexist_on_terminal(self, fs, fake_transport):
        fake_transport.existing_dirs.add("/foo")
        # No raise:
        fs.mkdir("/foo", exist_ok=True)

    def test_eexist_on_intermediate_segment_does_not_fail(self, fs, fake_transport):
        # /a already exists; /a/b/c is the real target.
        fake_transport.existing_dirs.add("/a")
        fs.mkdir("/a/b/c")  # exist_ok=False, but intermediate is fine
        mkdir_paths = [c[1] for c in fake_transport.calls if c[0] == "fs_mkdir"]
        assert mkdir_paths == ["/a", "/a/b", "/a/b/c"]

    def test_eexist_on_terminal_without_exist_ok_raises(self, fs, fake_transport):
        fake_transport.existing_dirs.add("/foo")
        with pytest.raises(RuntimeError, match="mkdir /foo"):
            fs.mkdir("/foo", exist_ok=False)

    def test_root_is_noop(self, fs, fake_transport):
        fs.mkdir("/")
        assert fake_transport.calls == []


# ---------------------------------------------------------------------------
# rmdir
# ---------------------------------------------------------------------------

class TestRmdir:
    def test_non_recursive_routes_to_fs_rmdir(self, fs, fake_transport):
        fs.rmdir("/empty")
        assert fake_transport.calls == [("fs_rmdir", "/empty")]

    def test_recursive_walks_and_removes(self, fs, fake_transport):
        # /root contains: file1, sub/  ;  /root/sub contains: file2
        fake_transport.listdir_response["/root"] = [
            DirEntry(name="file1", st_mode=0o100644, st_ino=0, st_size=10),
            DirEntry(name="sub", st_mode=0o040755, st_ino=0, st_size=0),
        ]
        fake_transport.listdir_response["/root/sub"] = [
            DirEntry(name="file2", st_mode=0o100644, st_ino=0, st_size=20),
        ]
        fs.rmdir("/root", recursive=True)

        # Order: list root → rmfile file1 → recurse into sub → list sub →
        # rmfile file2 → rmdir sub → rmdir root.
        assert fake_transport.calls == [
            ("fs_listdir", "/root"),
            ("fs_rmfile", "/root/file1"),
            ("fs_listdir", "/root/sub"),
            ("fs_rmfile", "/root/sub/file2"),
            ("fs_rmdir", "/root/sub"),
            ("fs_rmdir", "/root"),
        ]

    def test_rmdir_path_sanitized(self, fs):
        with pytest.raises(ValueError):
            fs.rmdir("/foo;rm")


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

class TestErrorMapping:
    def test_transport_error_from_listdir_wrapped(self, fs, fake_transport):
        fake_transport.errors[("fs_listdir", "/x")] = TransportError("bad")
        with pytest.raises(RuntimeError, match="list_files /x") as ei:
            fs.list_files("/x")
        assert isinstance(ei.value.__cause__, TransportError)

    def test_oserror_from_writefile_wrapped(self, fs, fake_transport):
        fake_transport.errors[("fs_writefile", "/x.txt")] = OSError(28, "ENOSPC")
        with pytest.raises(RuntimeError, match="write_file /x.txt"):
            fs.write_file("/x.txt", b"data", verify=False)


# ---------------------------------------------------------------------------
# sync_file(NEWEST) — fallback when device reports no mtime
# ---------------------------------------------------------------------------


class TestSyncFileNewestMtimeFallback:
    """sync_file(NEWEST) when device reports no mtime (TODO #8).

    MicroPython on littlefs without an RTC reports st_mtime=0;
    FileOperations.get_file_info converts that to FileInfo.mtime=None.
    sync_file's NEWEST branch then resolves remote mtime via
    ``remote_info.mtime or 0`` (file_ops.py:292), so any local file with a
    non-zero mtime wins. This is the documented fallback: local always
    wins when the device has no clock. These tests pin that contract so a
    future refactor (e.g. ``... or local_mtime`` to mean "no opinion → in
    sync") trips a guard rather than silently flipping direction.
    """

    @staticmethod
    def _make_local(tmp_path: Path, name: str = "main.py", body: bytes = b"print('host')") -> Path:
        local = tmp_path / name
        local.write_bytes(body)
        return local

    def test_remote_mtime_zero_uploads_local(self, fs, fake_transport, tmp_path):
        """End-to-end through fs_stat: device reports st_mtime=0, sync
        uploads. Validates the get_file_info → 0→None → 'or 0' → local-wins
        chain in one shot."""
        local = self._make_local(tmp_path)
        fake_transport.stat_response["/main.py"] = StatResult(0o100644, len(local.read_bytes()), st_mtime=0)
        fake_transport.hashfile_response["/main.py"] = hashlib.sha256(local.read_bytes()).digest()

        result = fs.sync_file(local, "/main.py")
        assert "Uploaded" in result and "local is newer" in result, result
        assert "fs_writefile" in call_names(fake_transport)

    def test_remote_mtime_None_via_stub_uploads_local(
        self, fs, fake_transport, tmp_path, monkeypatch
    ):
        """Direct stub of get_file_info → mtime=None. Locks the contract
        independently of the stat-tuple round-trip — if the FileInfo
        adapter ever stops mapping 0→None, this test still pins the
        sync_file behavior."""
        local = self._make_local(tmp_path)
        fake_transport.hashfile_response["/main.py"] = hashlib.sha256(local.read_bytes()).digest()
        monkeypatch.setattr(
            fs, "get_file_info",
            lambda path: FileInfo(name="main.py", size=len(local.read_bytes()), is_dir=False, mtime=None),
        )

        result = fs.sync_file(local, "/main.py")
        assert "Uploaded" in result and "local is newer" in result, result
        assert "fs_writefile" in call_names(fake_transport)

    def test_remote_None_and_local_zero_treated_as_in_sync(
        self, fs, fake_transport, tmp_path, monkeypatch
    ):
        """Edge case proving the fallback is symmetric, not absolute:
        BOTH sides resolve to 0 → equal → in sync, no transfer. Ensures
        a future "local always wins" interpretation doesn't sneak in.
        """
        import os
        local = tmp_path / "epoch.txt"
        local.write_bytes(b"x")
        os.utime(local, (0, 0))  # local mtime → 0
        monkeypatch.setattr(
            fs, "get_file_info",
            lambda path: FileInfo(name="epoch.txt", size=1, is_dir=False, mtime=None),
        )

        result = fs.sync_file(local, "/epoch.txt")
        assert "in sync" in result.lower(), result
        names = call_names(fake_transport)
        assert "fs_writefile" not in names
        assert "fs_readfile" not in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
