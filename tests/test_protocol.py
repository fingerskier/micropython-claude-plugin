"""Unit tests for the raw REPL / raw-paste protocol — no hardware required.

These exercise the bits that are most likely to break silently:
  - response framing parse (empty, stdout-only, stderr-only, both)
  - raw-paste write loop (window-based flow control, abort byte)
  - raw-paste fallback to legacy mode (on R\\x00 and on probe timeout)
  - TimeoutError on stalled reads (the review's #2)
"""

import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import (  # noqa: E402
    MicroPythonDevice,
    RawPasteNotSupported,
)


class FakeSerial:
    """In-memory pyserial stand-in controlled by test threads.

    Tests push device-side bytes via ``feed(...)`` and inspect what the
    driver wrote via ``written``. Reads block until data is available or
    the (per-call) timeout elapses, mirroring pyserial's behavior.
    """

    def __init__(self):
        self._read_buf = bytearray()
        self._cond = threading.Condition()
        self.written = bytearray()
        self.is_open = True
        self.timeout = 0.05

    # ------- driver-side API -------
    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._read_buf)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while not self._read_buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            out = bytes(self._read_buf[:size])
            del self._read_buf[:size]
            return out

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._read_buf.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    # ------- test-side API -------
    def feed(self, data: bytes) -> None:
        with self._cond:
            self._read_buf.extend(data)
            self._cond.notify_all()


def _make_device(fake: FakeSerial) -> MicroPythonDevice:
    dev = MicroPythonDevice(port="/dev/null", baudrate=115200)
    dev._serial = fake  # type: ignore[assignment]
    return dev


# ---------------------------------------------------------------------------
# Response framing
# ---------------------------------------------------------------------------

class TestParseExecuteResponse:
    def test_stdout_only(self):
        out, err = MicroPythonDevice._parse_execute_response(b"hello\x04\x04>")
        assert out == "hello"
        assert err == ""

    def test_stdout_and_stderr(self):
        out, err = MicroPythonDevice._parse_execute_response(
            b"ok\x04Traceback\n  ...\x04>"
        )
        assert out == "ok"
        assert err == "Traceback\n  ..."

    def test_empty(self):
        out, err = MicroPythonDevice._parse_execute_response(b"\x04\x04>")
        assert out == ""
        assert err == ""

    def test_stderr_only(self):
        out, err = MicroPythonDevice._parse_execute_response(b"\x04oops\x04>")
        assert out == ""
        assert err == "oops"

    def test_no_trailing_prompt(self):
        # Partial frame (shouldn't happen in practice, but parser must
        # not crash).
        out, err = MicroPythonDevice._parse_execute_response(b"partial")
        assert out == "partial"
        assert err == ""


# ---------------------------------------------------------------------------
# _read_until timeout semantics — the review's #2
# ---------------------------------------------------------------------------

class TestReadUntilTimeout:
    def test_raises_on_timeout(self):
        fake = FakeSerial()
        dev = _make_device(fake)
        with pytest.raises(TimeoutError):
            dev._read_until(b'\x04>', timeout=0.2)

    def test_returns_on_terminator(self):
        fake = FakeSerial()
        dev = _make_device(fake)
        fake.feed(b"hello\x04>")
        assert dev._read_until(b'\x04>', timeout=1.0) == b"hello\x04>"

    def test_terminator_split_across_reads(self):
        fake = FakeSerial()
        dev = _make_device(fake)

        def feeder():
            time.sleep(0.05)
            fake.feed(b"he")
            time.sleep(0.05)
            fake.feed(b"llo\x04")
            time.sleep(0.05)
            fake.feed(b">")

        t = threading.Thread(target=feeder)
        t.start()
        try:
            got = dev._read_until(b'\x04>', timeout=2.0)
            assert got == b"hello\x04>"
        finally:
            t.join()


# ---------------------------------------------------------------------------
# Raw-paste probe + fallback
# ---------------------------------------------------------------------------

class TestRawPasteProbe:
    def test_unsupported_response_raises_raw_paste_not_supported(self):
        fake = FakeSerial()
        dev = _make_device(fake)
        # Device replies R\x00 ("raw paste not supported").
        fake.feed(b"R\x00")
        with pytest.raises(RawPasteNotSupported):
            dev._execute_raw_paste("print(1)", timeout=1.0)
        # Driver should have written the probe sequence.
        assert fake.written.startswith(b'\x05A\x01')

    def test_probe_timeout_raises_raw_paste_not_supported(self):
        fake = FakeSerial()
        dev = _make_device(fake)
        # Device sends nothing — probe should time out and raise.
        with pytest.raises(RawPasteNotSupported):
            dev._execute_raw_paste("print(1)", timeout=0.2)

    def test_raw_paste_success_roundtrip(self):
        fake = FakeSerial()
        dev = _make_device(fake)

        code = b"print(42)"
        window = 32

        def device_side():
            # Probe response: supported + window size.
            fake.feed(b"R\x01" + struct.pack("<H", window))
            # Initial flow credit.
            fake.feed(b"\x01")
            # Wait until driver has written all code bytes + Ctrl-D.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                # End of paste is driver writing code + Ctrl-D after the
                # probe + window preamble (which was 3 bytes on the
                # written-by-driver side).
                if (len(fake.written) >=
                        len(b'\x05A\x01') + len(code) + 1):
                    break
                time.sleep(0.01)
            # Send end-of-data ack.
            fake.feed(b'\x04')
            # Send execution output: stdout=42\n, stderr empty.
            fake.feed(b"42\n\x04\x04>")

        t = threading.Thread(target=device_side)
        t.start()
        try:
            stdout, stderr = dev._execute_raw_paste(
                code.decode(), timeout=3.0
            )
        finally:
            t.join()

        assert stdout == "42\n"
        assert stderr == ""
