"""
Cross-device image roundtrip eval (TODO #1 from TODO.md).

Pulls the filesystem image from device A, restores it onto device B with
clean=True + allow_root_wipe=True, then asserts compare_image(B, golden)
yields empty `different`, `only_on_device`, and `only_in_image` buckets.
The `matching` bucket must contain every non-metadata entry from the
archive — confirming that the image format is portable across two
boards of the same family.

Device B is backed up before the wipe and restored after the test, so
the test is non-destructive against B's live filesystem. The restore
runs in a `finally` so it executes even when the assertion fails.

Usage:
    python tests/test_cross_device_image.py --port-a COM4 --port-b COM11

If --port-a / --port-b are omitted, the script auto-picks the first two
ports whose VID matches a known MicroPython vendor (RPi 0x2E8A, FTDI,
Pimoroni, Espressif).
"""

import argparse
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import MicroPythonDevice
from micropython_claude_plugin.image_ops import ImageOperations


_MP_VIDS = {0x2E8A, 0x16C0, 0x1A86, 0x10C4, 0x0403, 0x303A, 0x16D0}


def _autodetect_two_ports() -> tuple[str, str]:
    from serial.tools import list_ports
    candidates = [p.device for p in list_ports.comports() if p.vid in _MP_VIDS]
    if len(candidates) < 2:
        raise RuntimeError(
            f"Need 2 MicroPython ports; auto-detect found {candidates}. "
            "Pass --port-a / --port-b explicitly."
        )
    return candidates[0], candidates[1]


def _archive_payload_names(image_path: Path) -> set[str]:
    """Names of non-metadata file entries in the tarball."""
    names: set[str] = set()
    with tarfile.open(image_path, "r:*") as tar:
        for m in tar.getmembers():
            if m.name == ".micropython_image_metadata.json":
                continue
            if not m.isfile():
                continue
            names.add(m.name)
    return names


def run(port_a: str, port_b: str, baudrate: int = 115200) -> int:
    print(f"=== Cross-device image roundtrip ===")
    print(f"A (golden source): {port_a}")
    print(f"B (sacrificial target): {port_b}")
    print()

    a = MicroPythonDevice(port_a, baudrate)
    b = MicroPythonDevice(port_b, baudrate)
    a.connect()
    b.connect()
    a_ops = ImageOperations(a)
    b_ops = ImageOperations(b)

    workdir = Path(tempfile.mkdtemp(prefix="xdev_image_"))
    golden = workdir / "golden_from_a.tar.gz"
    b_backup = workdir / "b_backup.tar.gz"

    t0 = time.time()
    failed = False
    try:
        print(f"[1/5] Pull golden from A -> {golden}")
        meta_a = a_ops.pull_image(str(golden))
        print(f"      {meta_a.file_count} files, {meta_a.total_size} bytes")
        assert meta_a.file_count > 0, "Device A image is empty"

        print(f"[2/5] Backup B -> {b_backup}")
        meta_b = b_ops.pull_image(str(b_backup))
        print(f"      {meta_b.file_count} files, {meta_b.total_size} bytes")

        print(f"[3/5] Push golden onto B (clean=True, allow_root_wipe=True)")
        push_result = b_ops.push_image(
            str(golden),
            target_path="/",
            clean=True,
            allow_root_wipe=True,
        )
        print(
            f"      cleaned={push_result['cleaned']}, "
            f"files_written={push_result['files_written']}, "
            f"bytes_written={push_result['bytes_written']}, "
            f"errors={len(push_result['errors'])}"
        )
        assert push_result["cleaned"], "B was not wiped before restore"
        assert push_result["errors"] == [], f"Push errors: {push_result['errors']}"

        print(f"[4/5] Compare B vs golden image")
        diff = b_ops.compare_with_image(str(golden))
        expected = _archive_payload_names(golden)
        print(
            f"      matching={len(diff['matching'])}, "
            f"different={len(diff['different'])}, "
            f"only_on_device={len(diff['only_on_device'])}, "
            f"only_in_image={len(diff['only_in_image'])}"
        )

        assert diff["different"] == [], (
            f"Expected zero content/size diffs, got: {diff['different']}"
        )
        assert diff["only_on_device"] == [], (
            f"B has files not in image after wipe+restore: {diff['only_on_device']}"
        )
        assert diff["only_in_image"] == [], (
            f"Image has files not on B after restore: {diff['only_in_image']}"
        )
        assert set(diff["matching"]) == expected, (
            f"matching set != archive payload. "
            f"missing={expected - set(diff['matching'])}, "
            f"extra={set(diff['matching']) - expected}"
        )

        elapsed = int((time.time() - t0) * 1000)
        print(f"\n[PASS] Cross-device roundtrip clean ({elapsed}ms, "
              f"{len(diff['matching'])} files verified)")
    except AssertionError as e:
        failed = True
        print(f"\n[FAIL] {e}")
    except Exception as e:
        failed = True
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        print(f"\n[5/5] Restore B from backup (always runs)")
        try:
            restore = b_ops.push_image(
                str(b_backup),
                target_path="/",
                clean=True,
                allow_root_wipe=True,
            )
            print(
                f"      restored: files_written={restore['files_written']}, "
                f"errors={len(restore['errors'])}"
            )
            if restore["errors"]:
                print(f"      WARNING: restore errors: {restore['errors']}")
        except Exception as e:
            print(f"      WARNING: B restore failed: {e}")
            print(f"      Backup preserved at: {b_backup}")
            failed = True

        a.disconnect()
        b.disconnect()

    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port-a", help="Source device port (golden)")
    parser.add_argument("--port-b", help="Target device port (sacrificial)")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()

    if args.port_a and args.port_b:
        port_a, port_b = args.port_a, args.port_b
    else:
        port_a, port_b = _autodetect_two_ports()
        print(f"Auto-detected: A={port_a}, B={port_b}\n")

    return run(port_a, port_b, args.baudrate)


if __name__ == "__main__":
    sys.exit(main())
