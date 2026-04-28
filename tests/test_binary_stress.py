"""
Binary file round-trip stress (TODO #2 from TODO.md).

Writes os.urandom(N) payloads of 10 KiB, 64 KiB, and 256 KiB to the
device, reads them back, asserts byte-exact equality on the read side,
and confirms transport.fs_hashfile's device-side sha256 digest matches
the host sha256. Random payloads (rather than sequential patterns) catch
framing / escaping bugs that repeated-byte content would mask, and the
larger sizes exercise the chunked transfer path beyond the
2.5 KiB cases in test_hardware_eval.py.

Cleans up after itself even on failure (finally-block).

Usage:
    python tests/test_binary_stress.py                   # COM4 + COM11
    python tests/test_binary_stress.py --port COM4
    python tests/test_binary_stress.py --port COM4 --port COM11
"""

import argparse
import hashlib
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import MicroPythonDevice
from micropython_claude_plugin.file_ops import FileOperations


SIZES: list[tuple[str, int]] = [
    ("10KiB", 10 * 1024),
    ("64KiB", 64 * 1024),
    ("256KiB", 256 * 1024),
]


def _stress_one(file_ops: FileOperations, label: str, size: int) -> tuple[int, int]:
    """Single payload round-trip. Returns (write_ms, read_ms)."""
    payload = os.urandom(size)
    host_hash = hashlib.sha256(payload).hexdigest()
    remote = f"/stress_{label}.bin"

    t0 = time.time()
    # write_file(verify=True) internally re-hashes via fs_hashfile against
    # the host payload and raises on mismatch — that covers the write
    # side and the device-side fs_hashfile path in a single call.
    file_ops.write_file(remote, payload, verify=True)
    t_write_ms = int((time.time() - t0) * 1000)

    t0 = time.time()
    got = file_ops.read_file(remote)
    t_read_ms = int((time.time() - t0) * 1000)

    # Independent read-side check: byte-exact equality. Goes through
    # fs_readfile (a different code path from fs_hashfile), so a bug that
    # silently truncates / reorders bytes on read would surface here even
    # though fs_hashfile was happy.
    assert len(got) == size, (
        f"{label}: read length {len(got)} != written {size}"
    )
    if got != payload:
        first = next(
            (i for i in range(size) if got[i] != payload[i]), -1
        )
        raise AssertionError(
            f"{label}: byte mismatch (first diff at byte {first}, "
            f"host_sha256={host_hash})"
        )

    return t_write_ms, t_read_ms


def run(port: str, baudrate: int = 115200) -> int:
    print(f"=== Binary stress on {port} ===")
    device = MicroPythonDevice(port, baudrate)
    device.connect()
    # Some boards launch main.py at boot which keeps printing to stdout
    # and collides with raw REPL framing for long transfers. interrupt()
    # sends Ctrl-C and drains until quiescent.
    device.interrupt()
    file_ops = FileOperations(device)

    failed = False
    try:
        for label, size in SIZES:
            try:
                t_w, t_r = _stress_one(file_ops, label, size)
                rate_w = size / max(t_w, 1) * 1000 / 1024
                rate_r = size / max(t_r, 1) * 1000 / 1024
                print(
                    f"[PASS] {label:7} {size:>7}B  "
                    f"write={t_w:>5}ms ({rate_w:5.1f} KiB/s)  "
                    f"read={t_r:>5}ms ({rate_r:5.1f} KiB/s)"
                )
            except AssertionError as e:
                print(f"[FAIL] {label}: {e}")
                failed = True
            except Exception as e:
                print(f"[ERROR] {label}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed = True
    finally:
        for label, _ in SIZES:
            try:
                p = f"/stress_{label}.bin"
                if file_ops.file_exists(p):
                    file_ops.delete_file(p)
            except Exception:
                pass
        device.disconnect()

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--port", action="append",
        help="Serial port (may be repeated). Default: COM4 + COM11.",
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()

    ports = args.port or ["COM4", "COM11"]
    rc = 0
    for p in ports:
        rc |= run(p, args.baudrate)
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
