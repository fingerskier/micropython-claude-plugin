"""Hardware sanity check for sync_file(NEWEST) mtime semantics (TODO #8).

The unit tests in test_fileops_adapter.py::TestSyncFileNewestMtimeFallback
pin the fallback contract (remote mtime None → 0 → local wins). This
test confirms the contract still holds end-to-end against a real device.

Two regimes the test handles:

  1. Device reports a non-zero mtime (Pimoroni RP2040 ships with a
     pseudo-RTC that increments a counter; mtime ≈ 2021 epoch). In this
     case the host's current Unix mtime is always > device mtime, so
     local wins. We exercise the upload branch and assert the read-back
     payload matches host bytes.

  2. Device reports mtime=0 (truly RTC-less builds). FileOperations maps
     0→None; sync_file falls back to 0; local wins again. Same
     assertion path.

Either way, NEWEST must upload host bytes to the device. The test prints
which regime applied for diagnostic value.

Usage:
    python tests/test_sync_file_newest_hw.py            # auto-detect single MP port
    python tests/test_sync_file_newest_hw.py --port COM4
"""

import argparse
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import MicroPythonDevice
from micropython_claude_plugin.file_ops import FileOperations, SyncDirection


_MP_VIDS = {0x2E8A, 0x16C0, 0x1A86, 0x10C4, 0x0403, 0x303A, 0x16D0}


def _autodetect_port() -> str:
    from serial.tools import list_ports
    candidates = [p.device for p in list_ports.comports() if p.vid in _MP_VIDS]
    if not candidates:
        raise RuntimeError(
            f"No MicroPython port detected. Pass --port. Saw: "
            f"{[p.device for p in list_ports.comports()]}"
        )
    return candidates[0]


REMOTE_PATH = f"/_sync_newest_hw_{os.getpid()}.txt"


def run(port: str) -> int:
    print(f"=== sync_file(NEWEST) mtime fallback HW: {port} ===")
    d = MicroPythonDevice(port)
    d.connect()
    d.interrupt()
    fo = FileOperations(d)

    failed = False
    try:
        # 1. Write a stale "v1" payload to the device.
        fo.write_file(REMOTE_PATH, b"v1-device", verify=True)
        info = fo.get_file_info(REMOTE_PATH)
        device_mtime = info.mtime if info else None
        regime = "non-zero" if device_mtime else "zero/None"
        print(f"[INFO] device mtime after write: {device_mtime!r}  ({regime})")

        # 2. Wait briefly so a freshly-written local file's mtime is
        #    monotonically later (matters in the rare case where device
        #    mtime is a very recent host-side epoch — not the case on
        #    Pimoroni RP2040, but we're being defensive).
        time.sleep(0.05)

        # 3. Call sync_file(NEWEST) with a local "v2" payload.
        with tempfile.TemporaryDirectory(prefix="syncnewest_") as td:
            local = Path(td) / "payload.txt"
            local.write_bytes(b"v2-host")
            result = fo.sync_file(local, REMOTE_PATH, SyncDirection.NEWEST)
            print(f"[INFO] sync_file result: {result}")

            # 4. Local mtime is current Unix time → greater than any
            #    plausible device mtime → upload should win.
            assert "Uploaded" in result and "local is newer" in result, (
                f"Expected upload (local newer); got: {result!r}. "
                f"device_mtime={device_mtime!r}"
            )

        # 5. Read back from device and verify host bytes landed.
        got = fo.read_file(REMOTE_PATH)
        assert got == b"v2-host", f"Read-back mismatch: {got!r}"
        print(f"[PASS] sync_file(NEWEST) uploaded host bytes (regime={regime})")

    except AssertionError as e:
        failed = True
        print(f"[FAIL] {e}")
    except Exception as e:
        failed = True
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        try:
            if fo.file_exists(REMOTE_PATH):
                fo.delete_file(REMOTE_PATH)
        except Exception:
            pass
        d.disconnect()

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()
    port = args.port or _autodetect_port()
    return run(port)


if __name__ == "__main__":
    sys.exit(main())
