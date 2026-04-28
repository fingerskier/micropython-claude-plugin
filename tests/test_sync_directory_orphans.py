"""Hardware test for sync_directory + delete_orphans (TODO #5).

Covers the three documented direction × delete_orphans cases on real
hardware against a sandbox directory (default ``/sync_orphan_test``):

  - UPLOAD: remote orphans (not in local) are removed when
    ``delete_orphans=True``, kept otherwise.
  - DOWNLOAD: local orphans (not on device) are removed when
    ``delete_orphans=True``, kept otherwise.
  - NEWEST + delete_orphans=True is a no-op (per
    ``file_ops.py:389``) and surfaces an explanatory entry in the
    returned results list.

Cleans the sandbox directory before AND after each subtest, on the
device and on the host. Designed to leave the device's normal
filesystem untouched.

Usage:
    python tests/test_sync_directory_orphans.py --port COM4
"""

import argparse
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import MicroPythonDevice
from micropython_claude_plugin.file_ops import FileOperations, SyncDirection


REMOTE_SANDBOX = "/sync_orphan_test"


def _wipe_remote_sandbox(file_ops: FileOperations) -> None:
    if file_ops.file_exists(REMOTE_SANDBOX):
        try:
            file_ops.rmdir(REMOTE_SANDBOX, recursive=True)
        except Exception:
            pass


def _list_remote_files(file_ops: FileOperations) -> set[str]:
    if not file_ops.file_exists(REMOTE_SANDBOX):
        return set()
    out: set[str] = set()
    for entry in file_ops.list_files(REMOTE_SANDBOX):
        if not entry.is_dir:
            out.add(entry.name)
    return out


def _list_local_files(local_dir: Path) -> set[str]:
    return {f.name for f in local_dir.iterdir() if f.is_file()}


def _make_local(local_dir: Path, names: list[str]) -> None:
    for n in names:
        (local_dir / n).write_bytes(f"content_of_{n}".encode())


def case_upload_orphan(file_ops: FileOperations, local_dir: Path) -> None:
    """UPLOAD + delete_orphans deletes remote files missing locally."""
    _wipe_remote_sandbox(file_ops)

    _make_local(local_dir, ["a.txt", "b.txt", "c.txt"])

    # Initial sync — no orphan delete; remote should mirror local.
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.UPLOAD,
        delete_orphans=False,
    )
    assert _list_remote_files(file_ops) == {"a.txt", "b.txt", "c.txt"}, (
        f"After initial UPLOAD: {_list_remote_files(file_ops)}"
    )

    # Delete b.txt locally, run UPLOAD without orphan delete — b.txt
    # MUST persist on device (proves delete_orphans=False is a real
    # opt-in, not the default behavior).
    (local_dir / "b.txt").unlink()
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.UPLOAD,
        delete_orphans=False,
    )
    assert _list_remote_files(file_ops) == {"a.txt", "b.txt", "c.txt"}, (
        f"UPLOAD w/o orphan-delete should keep b.txt; got {_list_remote_files(file_ops)}"
    )

    # Now flip delete_orphans=True; b.txt must disappear from device.
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.UPLOAD,
        delete_orphans=True,
    )
    assert _list_remote_files(file_ops) == {"a.txt", "c.txt"}, (
        f"UPLOAD w/ orphan-delete should remove b.txt; got {_list_remote_files(file_ops)}"
    )


def case_download_orphan(file_ops: FileOperations, local_dir: Path) -> None:
    """DOWNLOAD + delete_orphans deletes local files missing on device."""
    _wipe_remote_sandbox(file_ops)
    for f in local_dir.iterdir():
        if f.is_file():
            f.unlink()

    # Seed device by uploading three files, then drop one device-side.
    _make_local(local_dir, ["x.txt", "y.txt", "z.txt"])
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.UPLOAD,
        delete_orphans=False,
    )
    file_ops.delete_file(f"{REMOTE_SANDBOX}/y.txt")
    assert _list_remote_files(file_ops) == {"x.txt", "z.txt"}

    # DOWNLOAD without orphan-delete keeps the local stragglers.
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.DOWNLOAD,
        delete_orphans=False,
    )
    assert _list_local_files(local_dir) == {"x.txt", "y.txt", "z.txt"}, (
        f"DOWNLOAD w/o orphan-delete should keep y.txt; got {_list_local_files(local_dir)}"
    )

    # DOWNLOAD with orphan-delete removes the local copy of y.txt.
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.DOWNLOAD,
        delete_orphans=True,
    )
    assert _list_local_files(local_dir) == {"x.txt", "z.txt"}, (
        f"DOWNLOAD w/ orphan-delete should remove y.txt; got {_list_local_files(local_dir)}"
    )


def case_newest_orphan_noop(file_ops: FileOperations, local_dir: Path) -> None:
    """NEWEST + delete_orphans is a documented no-op with a warning."""
    _wipe_remote_sandbox(file_ops)
    for f in local_dir.iterdir():
        if f.is_file():
            f.unlink()

    _make_local(local_dir, ["m.txt", "n.txt"])
    file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.UPLOAD,
        delete_orphans=False,
    )
    # Diverge: drop n.txt locally and m.txt on device.
    (local_dir / "n.txt").unlink()
    file_ops.delete_file(f"{REMOTE_SANDBOX}/m.txt")

    before_local = _list_local_files(local_dir)
    before_remote = _list_remote_files(file_ops)

    results = file_ops.sync_directory(
        local_dir, REMOTE_SANDBOX,
        direction=SyncDirection.NEWEST,
        delete_orphans=True,
    )

    # After NEWEST sync the missing-on-one-side files get copied across
    # (NEWEST fills both sides in), but no DELETES happen — the warning
    # entry must appear in the results list.
    warned = any("delete_orphans ignored" in line.lower() for line in results)
    assert warned, (
        f"Expected delete_orphans-ignored warning in results, got: {results}"
    )

    # Files that existed before still exist on their original side
    # (NEWEST never deletes, only copies). The other side may have
    # gained the missing file — that's correct fill-in behavior, not
    # an orphan-delete.
    after_local = _list_local_files(local_dir)
    after_remote = _list_remote_files(file_ops)
    assert before_local <= after_local, (
        f"NEWEST removed local file(s): before={before_local} after={after_local}"
    )
    assert before_remote <= after_remote, (
        f"NEWEST removed remote file(s): before={before_remote} after={after_remote}"
    )


def run(port: str, baudrate: int = 115200) -> int:
    print(f"=== sync_directory + delete_orphans on {port} ===")
    device = MicroPythonDevice(port, baudrate)
    device.connect()
    device.interrupt()
    file_ops = FileOperations(device)

    failed = False
    cases = [
        ("UPLOAD orphan-delete", case_upload_orphan),
        ("DOWNLOAD orphan-delete", case_download_orphan),
        ("NEWEST orphan-delete no-op", case_newest_orphan_noop),
    ]
    for name, fn in cases:
        with tempfile.TemporaryDirectory(prefix="syncorph_") as tmp:
            local_dir = Path(tmp)
            try:
                fn(file_ops, local_dir)
                print(f"[PASS] {name}")
            except AssertionError as e:
                print(f"[FAIL] {name}: {e}")
                failed = True
            except Exception as e:
                print(f"[ERROR] {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed = True
            finally:
                _wipe_remote_sandbox(file_ops)

    device.disconnect()
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", default="COM4")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()
    return run(args.port, args.baudrate)


if __name__ == "__main__":
    sys.exit(main())
