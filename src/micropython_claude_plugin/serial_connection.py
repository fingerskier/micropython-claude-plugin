"""Serial connection manager for MicroPython devices.

Implements the mpremote-style raw REPL protocol:
  - Handshake-driven raw REPL entry (no blind sleeps)
  - Raw-paste mode (windowed flow control) for fast code/data transfer
  - Persistent raw REPL sessions so many executes share one enter/exit
  - Timeouts raise instead of returning partial data
  - Thread-safe serial I/O via an internal RLock
  - Bounded reconnect on transient USB disruption
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import serial
import serial.tools.list_ports


class RawPasteNotSupported(Exception):
    """The connected firmware does not support raw-paste mode."""


@dataclass
class DeviceInfo:
    """Information about a connected device."""
    port: str
    description: str
    hwid: str
    vid: int | None = None
    pid: int | None = None


class MicroPythonDevice:
    """Manages serial connection to a MicroPython device."""

    # REPL control characters
    CTRL_A = b'\x01'  # Enter raw REPL
    CTRL_B = b'\x02'  # Exit raw REPL
    CTRL_C = b'\x03'  # Interrupt
    CTRL_D = b'\x04'  # Soft reset / execute in raw REPL
    CTRL_E = b'\x05'  # Raw-paste entry prefix

    RAW_REPL_BANNER = b'raw REPL; CTRL-B to exit\r\n>'
    NORMAL_REPL_PROMPT = b'>>> '

    # Default retry policy for transient SerialException during I/O.
    RECONNECT_RETRIES = 1
    RECONNECT_BACKOFF = 0.5

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 0.05,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: serial.Serial | None = None
        self._raw_repl_active = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self) -> None:
        if self.is_connected:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )
        # Let USB CDC settle, then flush framing bytes.
        time.sleep(0.1)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        self._raw_repl_active = False

    def disconnect(self) -> None:
        with self._lock:
            if self._raw_repl_active:
                try:
                    self._serial.write(self.CTRL_B)  # type: ignore[union-attr]
                except Exception:
                    pass
                self._raw_repl_active = False
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

    def _ensure_connected(self) -> serial.Serial:
        if not self.is_connected or self._serial is None:
            raise ConnectionError("Not connected to device")
        return self._serial

    def _reopen(self) -> None:
        """Close and re-open the port, preserving settings. Invalidates raw REPL."""
        try:
            if self._serial:
                self._serial.close()
        except Exception:
            pass
        self._serial = None
        self._raw_repl_active = False
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )
        time.sleep(0.1)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    # ------------------------------------------------------------------
    # Low-level I/O (raise on timeout, thread-safe, bytearray-based)
    # ------------------------------------------------------------------

    def _read_until(self, terminator: bytes, timeout: float = 5.0) -> bytes:
        """Read from serial until terminator appears; raise TimeoutError on timeout.

        On timeout, the raised ``TimeoutError`` carries the full partial
        buffer on ``.partial`` (as bytes) in addition to a truncated
        preview in the message. Callers can log/inspect ``e.partial`` to
        understand how far the exchange got.
        """
        ser = self._ensure_connected()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        # Use short blocking reads so we don't burn CPU.
        prev_timeout = ser.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    err = TimeoutError(
                        f"Timeout waiting for {terminator!r}; "
                        f"got {bytes(buf[-200:])!r} (len={len(buf)})"
                    )
                    err.partial = bytes(buf)  # type: ignore[attr-defined]
                    raise err
                ser.timeout = min(0.1, remaining)
                n = ser.in_waiting or 1
                chunk = ser.read(n)
                if chunk:
                    buf.extend(chunk)
                    if terminator in buf:
                        return bytes(buf)
        finally:
            ser.timeout = prev_timeout

    def _read_exact(self, n: int, timeout: float = 5.0) -> bytes:
        """Read exactly n bytes or raise TimeoutError (with .partial)."""
        ser = self._ensure_connected()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        prev_timeout = ser.timeout
        try:
            while len(buf) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    err = TimeoutError(
                        f"Timeout reading {n} bytes (got {len(buf)}): "
                        f"{bytes(buf)!r}"
                    )
                    err.partial = bytes(buf)  # type: ignore[attr-defined]
                    raise err
                ser.timeout = min(0.1, remaining)
                chunk = ser.read(n - len(buf))
                if chunk:
                    buf.extend(chunk)
            return bytes(buf)
        finally:
            ser.timeout = prev_timeout

    def _drain_idle(self, idle_secs: float = 0.1, max_wait: float = 1.0) -> bytes:
        """Read until no more data arrives for `idle_secs`, or max_wait elapses."""
        ser = self._ensure_connected()
        buf = bytearray()
        deadline = time.monotonic() + max_wait
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            if ser.in_waiting:
                buf.extend(ser.read(ser.in_waiting))
                last_data = time.monotonic()
            elif time.monotonic() - last_data >= idle_secs:
                break
            else:
                time.sleep(0.005)
        return bytes(buf)

    # ------------------------------------------------------------------
    # REPL / raw REPL
    # ------------------------------------------------------------------

    def interrupt(self) -> None:
        """Send Ctrl-C; drain what the device emits before discarding.

        Draining (vs blindly resetting the input buffer) preserves any
        traceback the running program was in the middle of printing, which
        matters for debugging.
        """
        with self._lock:
            ser = self._ensure_connected()
            ser.write(self.CTRL_C)
            self._drain_idle(idle_secs=0.1, max_wait=0.5)
            self._raw_repl_active = False

    def enter_raw_repl(self) -> None:
        """Idempotent raw REPL entry, handshake-driven."""
        with self._lock:
            if self._raw_repl_active:
                return
            ser = self._ensure_connected()

            # Interrupt any running program, drain fully.
            ser.write(self.CTRL_C + self.CTRL_C)
            self._drain_idle(idle_secs=0.05, max_wait=0.5)

            # Enter raw REPL. The \r nudges devices that need a keystroke
            # after an interrupt.
            ser.write(b'\r' + self.CTRL_A)
            try:
                self._read_until(self.RAW_REPL_BANNER, timeout=2.0)
            except TimeoutError as e:
                raise RuntimeError(f"Failed to enter raw REPL: {e}") from e
            self._raw_repl_active = True

    def exit_raw_repl(self) -> None:
        with self._lock:
            if not self._raw_repl_active:
                return
            ser = self._ensure_connected()
            try:
                ser.write(self.CTRL_B)
                self._drain_idle(idle_secs=0.05, max_wait=0.3)
            finally:
                self._raw_repl_active = False

    @contextmanager
    def raw_repl_session(self) -> Generator[None, None, None]:
        """Keep raw REPL open across many executes.

        Nesting is safe: only the outermost call enters/exits. Callers that
        execute many operations (file sync, image pull/push) should wrap
        their work in this to avoid per-call protocol overhead.
        """
        with self._lock:
            owns = not self._raw_repl_active
            if owns:
                self.enter_raw_repl()
            try:
                yield
            finally:
                if owns:
                    self.exit_raw_repl()

    def soft_reset(self) -> str:
        """Soft-reset the device. Leaves it in normal REPL mode."""
        with self._lock:
            self.interrupt()
            self.enter_raw_repl()
            ser = self._ensure_connected()
            ser.write(self.CTRL_D)
            # After Ctrl-D in raw REPL the device reboots and prints its
            # boot banner + main.py output. If main.py runs forever we
            # won't see the normal REPL prompt; return whatever we have.
            self._raw_repl_active = False
            try:
                out = self._read_until(self.NORMAL_REPL_PROMPT, timeout=5.0)
            except TimeoutError:
                out = b""
            return out.decode('utf-8', errors='replace')

    # ------------------------------------------------------------------
    # Execute (raw-paste preferred, legacy fallback)
    # ------------------------------------------------------------------

    def execute_raw(self, code: str, timeout: float = 10.0) -> tuple[str, str]:
        """Execute Python code, return (stdout, stderr)."""
        with self._lock:
            with self.raw_repl_session():
                return self._do_execute(code, timeout)

    def execute(self, code: str, timeout: float = 10.0) -> str:
        """Execute and return stdout; raise RuntimeError if stderr is non-empty."""
        stdout, stderr = self.execute_raw(code, timeout)
        if stderr:
            raise RuntimeError(f"Execution error: {stderr.strip()}")
        return stdout

    def _do_execute(self, code: str, timeout: float) -> tuple[str, str]:
        for attempt in range(self.RECONNECT_RETRIES + 1):
            try:
                try:
                    return self._execute_raw_paste(code, timeout)
                except RawPasteNotSupported:
                    return self._execute_legacy(code, timeout)
            except serial.SerialException as e:
                if attempt >= self.RECONNECT_RETRIES:
                    raise
                # USB re-enumeration: try once to recover.
                time.sleep(self.RECONNECT_BACKOFF)
                self._reopen()
                self.enter_raw_repl()
        raise RuntimeError("unreachable")

    def _execute_raw_paste(self, code: str, timeout: float) -> tuple[str, str]:
        ser = self._ensure_connected()

        # Probe for raw-paste support.
        ser.write(self.CTRL_E + b'A' + self.CTRL_A)
        try:
            header = self._read_exact(2, timeout=1.0)
        except TimeoutError as e:
            # Firmware didn't respond to the probe within 1 s — assume
            # it's an older port that silently ignored the prefix bytes
            # (and is still at the raw REPL prompt waiting for code).
            raise RawPasteNotSupported("probe timeout") from e
        if header != b'R\x01':
            # Stock reply is b'R\x00' (unsupported) or b'R\x01' (supported).
            # Anything else: bail to legacy.
            if header == b'R\x00':
                self._drain_idle(idle_secs=0.05, max_wait=0.3)
            raise RawPasteNotSupported(header)

        # Window size (little-endian).
        win_bytes = self._read_exact(2, timeout=1.0)
        window_total = win_bytes[0] | (win_bytes[1] << 8)
        window_remain = window_total

        code_bytes = code.encode('utf-8')
        i = 0
        while i < len(code_bytes):
            # Drain any flow-control bytes before sending more.
            while window_remain == 0 or ser.in_waiting:
                b = self._read_exact(1, timeout=timeout)
                if b == b'\x01':
                    window_remain += window_total
                elif b == b'\x04':
                    # Device asked to abort — acknowledge and fail.
                    try:
                        ser.write(self.CTRL_D)
                    except Exception:
                        pass
                    raise RuntimeError("Device aborted raw-paste transfer")
                else:
                    # Unexpected byte; treat as corrupted stream.
                    raise RuntimeError(
                        f"Unexpected flow-control byte: {b!r}"
                    )
            n = min(window_remain, len(code_bytes) - i)
            ser.write(code_bytes[i:i + n])
            i += n
            window_remain -= n

        # Signal end-of-input.
        ser.write(self.CTRL_D)

        # Device acknowledges end-of-input with a single \x04 before it
        # starts executing. Consume it so it doesn't appear as the leading
        # byte of stdout.
        ack = self._read_exact(1, timeout=timeout)
        if ack != b'\x04':
            raise RuntimeError(
                f"Expected \\x04 ack after raw-paste end, got {ack!r}"
            )

        # Execution output framing:
        #   <stdout>\x04<stderr>\x04>
        data = self._read_until(b'\x04>', timeout=timeout)
        return self._parse_execute_response(data)

    def _execute_legacy(self, code: str, timeout: float) -> tuple[str, str]:
        """Fallback path for ports that don't support raw-paste."""
        ser = self._ensure_connected()
        ser.write(code.encode('utf-8'))
        ser.write(self.CTRL_D)
        data = self._read_until(b'\x04>', timeout=timeout)
        # Legacy path prefixes with 'OK'.
        if data.startswith(b'OK'):
            data = data[2:]
        return self._parse_execute_response(data)

    @staticmethod
    def _parse_execute_response(data: bytes) -> tuple[str, str]:
        # Strip trailing \x04> framing.
        if data.endswith(b'\x04>'):
            data = data[:-2]
        parts = data.split(b'\x04', 1)
        stdout = parts[0].decode('utf-8', errors='replace')
        stderr = parts[1].decode('utf-8', errors='replace') if len(parts) > 1 else ''
        return stdout, stderr

    # ------------------------------------------------------------------
    # Raw byte passthrough (used by streaming)
    # ------------------------------------------------------------------

    def read_available(self) -> bytes:
        # Same lock as write(): reads must not race raw-REPL exchanges or
        # the streaming reader would steal bytes meant for the protocol.
        with self._lock:
            ser = self._ensure_connected()
            if ser.in_waiting:
                return ser.read(ser.in_waiting)
            return b''

    def write(self, data: bytes) -> int:
        with self._lock:
            ser = self._ensure_connected()
            return ser.write(data)

    def send_line(self, line: str) -> None:
        with self._lock:
            ser = self._ensure_connected()
            ser.write((line + '\r\n').encode('utf-8'))


@contextmanager
def device_connection(
    port: str,
    baudrate: int = 115200,
    timeout: float = 0.05,
) -> Generator[MicroPythonDevice, None, None]:
    device = MicroPythonDevice(port, baudrate, timeout)
    device.connect()
    try:
        yield device
    finally:
        device.disconnect()


def list_devices() -> list[DeviceInfo]:
    devices = []
    for port_info in serial.tools.list_ports.comports():
        devices.append(DeviceInfo(
            port=port_info.device,
            description=port_info.description,
            hwid=port_info.hwid,
            vid=port_info.vid,
            pid=port_info.pid,
        ))
    return devices


def find_micropython_devices() -> list[DeviceInfo]:
    # (vid, pid) — pid=None matches any PID for that VID.
    micropython_ids = [
        (0x2E8A, None),    # Raspberry Pi (Pico / Pico W)
        (0x1A86, 0x7523),  # CH340
        (0x10C4, 0xEA60),  # CP210x
        (0x0403, 0x6001),  # FTDI
        (0x303A, None),    # Espressif
    ]
    out = []
    for device in list_devices():
        for vid, pid in micropython_ids:
            if device.vid == vid and (pid is None or device.pid == pid):
                out.append(device)
                break
    return out
