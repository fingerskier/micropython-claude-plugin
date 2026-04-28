"""Concurrent two-device session isolation (TODO #6).

Two `MicroPythonDevice` instances exist in the same process at the same
time and must not cross-talk. Specifically the test asserts:

  - per-instance identity for `_transport`, `transport.serial`, and `_lock`
    (no module-/class-scoped state silently shared between instances);
  - opening a `raw_repl_session` on device A does NOT flip
    `transport.in_raw_repl` on device B;
  - parallel `list_files("/")` on two threads, one per device, both
    succeed and return their own data;
  - a unique marker file written on device A is NOT visible in
    device B's listing — the strongest cross-talk smoke test, since
    a port mix-up or shared transport would leak A's filesystem
    into B's view.

This guards against regressions in `serial_connection.py` if the lock
or transport handle ever becomes module-scoped.

Usage:
    python tests/test_concurrent_devices.py                  # auto-detect
    python tests/test_concurrent_devices.py --port-a COM4 --port-b COM11
"""

import argparse
import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import MicroPythonDevice
from micropython_claude_plugin.file_ops import FileOperations


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


def case_distinct_per_device_state(a: MicroPythonDevice, b: MicroPythonDevice) -> None:
    """transport / transport.serial / _lock must be distinct objects."""
    assert a._transport is not b._transport, (
        "Both devices share the same _transport object — class-/module-"
        "scoped transport regression."
    )
    assert a.transport.serial is not b.transport.serial, (
        "Both devices share the same underlying pyserial handle."
    )
    assert a._lock is not b._lock, (
        "Both devices share the same RLock — a single hung op would "
        "freeze concurrent calls on the other device."
    )
    assert a.port != b.port, "Test setup invalid: same port on both"


def case_raw_repl_flag_isolation(a: MicroPythonDevice, b: MicroPythonDevice) -> None:
    """Entering raw REPL on A must not flip B's in_raw_repl flag."""
    with a.raw_repl_session():
        assert a.transport.in_raw_repl is True, "A's raw REPL flag not set"
        assert b.transport.in_raw_repl is False, (
            "B's in_raw_repl flag was flipped by A's session — "
            "shared transport regression."
        )
    assert a.transport.in_raw_repl is False, "A still in raw REPL after exit"


def case_parallel_list_files(a: MicroPythonDevice, b: MicroPythonDevice) -> None:
    """list_files('/') on both devices in parallel — both must succeed."""
    a_ops = FileOperations(a)
    b_ops = FileOperations(b)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def _list(label: str, ops: FileOperations) -> None:
        try:
            results[label] = [e.name for e in ops.list_files("/")]
        except BaseException as e:
            errors[label] = e

    ta = threading.Thread(target=_list, args=("a", a_ops))
    tb = threading.Thread(target=_list, args=("b", b_ops))
    ta.start(); tb.start()
    ta.join(timeout=20); tb.join(timeout=20)

    assert not ta.is_alive() and not tb.is_alive(), (
        "Parallel list_files threads did not finish within 20s — "
        "possible cross-device deadlock."
    )
    assert not errors, f"Parallel list_files raised: {errors}"
    assert isinstance(results.get("a"), list) and len(results["a"]) > 0
    assert isinstance(results.get("b"), list) and len(results["b"]) > 0


def case_cross_device_marker_invisibility(a: MicroPythonDevice, b: MicroPythonDevice) -> None:
    """Marker file written on A must not appear in B's listing.

    This is the highest-signal cross-talk check: if the two devices
    shared a transport or got their writes routed to the wrong port,
    A's marker would either appear in B's listing (port mix-up) or
    nothing would land at all (transport corruption).
    """
    a_ops = FileOperations(a)
    b_ops = FileOperations(b)

    marker = f"/_concurrent_test_marker_{os.getpid()}.txt"
    payload = b"a-only-payload"
    a_ops.write_file(marker, payload)
    try:
        a_names = [e.name for e in a_ops.list_files("/")]
        b_names = [e.name for e in b_ops.list_files("/")]
        assert marker.lstrip("/") in a_names, (
            f"Marker not visible on A after write: {a_names}"
        )
        assert marker.lstrip("/") not in b_names, (
            f"Marker leaked to B's listing — write routed to wrong port. "
            f"b_names={b_names}"
        )
        # And confirm read on A returns the right payload (a write that
        # landed on B and accidentally read back on A would still pass
        # the listing check above — but we'd see corrupted bytes).
        assert a_ops.read_file(marker) == payload, "A's read corrupted"
    finally:
        try:
            if a_ops.file_exists(marker):
                a_ops.delete_file(marker)
        except Exception:
            pass


def run(port_a: str, port_b: str, baudrate: int = 115200) -> int:
    print(f"=== Concurrent two-device isolation: A={port_a} B={port_b} ===")
    a = MicroPythonDevice(port_a, baudrate)
    b = MicroPythonDevice(port_b, baudrate)
    a.connect(); b.connect()
    a.interrupt(); b.interrupt()

    failed = False
    cases = [
        ("Distinct per-device state", case_distinct_per_device_state),
        ("Raw REPL flag isolation", case_raw_repl_flag_isolation),
        ("Parallel list_files", case_parallel_list_files),
        ("Cross-device marker invisibility", case_cross_device_marker_invisibility),
    ]
    for name, fn in cases:
        t0 = time.time()
        try:
            fn(a, b)
            ms = int((time.time() - t0) * 1000)
            print(f"[PASS] {name} ({ms}ms)")
        except AssertionError as e:
            failed = True
            print(f"[FAIL] {name}: {e}")
        except Exception as e:
            failed = True
            print(f"[ERROR] {name}: {type(e).__name__}: {e}")
            traceback.print_exc()

    a.disconnect(); b.disconnect()
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port-a")
    parser.add_argument("--port-b")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()
    if args.port_a and args.port_b:
        port_a, port_b = args.port_a, args.port_b
    else:
        port_a, port_b = _autodetect_two_ports()
        print(f"Auto-detected: A={port_a}, B={port_b}")
    return run(port_a, port_b, args.baudrate)


if __name__ == "__main__":
    sys.exit(main())
