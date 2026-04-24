"""Serial connection manager for MicroPython devices.

Thin facade over ``mpremote.transport_serial.SerialTransport``: mpremote
owns the raw-REPL / raw-paste protocol, and this module exposes the same
public API the rest of the plugin already depends on. Streaming code that
needs raw byte-level access reaches through ``transport.serial`` (the
underlying ``serial.Serial``) via ``read_available`` / ``write`` /
``send_line``; everything else goes through ``transport``'s own methods.
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import serial
import serial.tools.list_ports
from mpremote.transport import TransportError
from mpremote.transport_serial import SerialTransport


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
        self._transport: SerialTransport | None = None
        # RLock guards the streaming reader thread (which touches
        # transport.serial directly) against tool calls that drive
        # transport.exec_raw / transport.fs_*.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return (
            self._transport is not None
            and self._transport.serial is not None
            and self._transport.serial.is_open
        )

    def connect(self) -> None:
        if self.is_connected:
            return
        try:
            self._transport = SerialTransport(self.port, baudrate=self.baudrate)
        except (TransportError, serial.SerialException, OSError) as e:
            self._transport = None
            raise ConnectionError(f"Failed to open {self.port}: {e}") from e
        # Honor the caller's read timeout for any direct serial.read() calls
        # the streaming layer makes. mpremote's own protocol calls pass
        # explicit timeouts to read_until.
        self._transport.serial.timeout = self.timeout
        # Let USB CDC settle, then flush framing bytes.
        time.sleep(0.1)
        self._transport.serial.reset_input_buffer()
        self._transport.serial.reset_output_buffer()

    def disconnect(self) -> None:
        with self._lock:
            if self._transport is None:
                return
            try:
                if self._transport.in_raw_repl:
                    try:
                        self._transport.exit_raw_repl()
                    except Exception:
                        pass
                self._transport.close()
            except Exception:
                pass
            self._transport = None

    def _ensure_transport(self) -> SerialTransport:
        if self._transport is None or not self.is_connected:
            raise ConnectionError("Not connected to device")
        return self._transport

    def _ensure_connected(self) -> serial.Serial:
        """Backwards-compat: return the underlying pyserial handle."""
        return self._ensure_transport().serial

    def _reopen(self) -> None:
        """Close and re-open the port. Invalidates raw REPL state."""
        try:
            if self._transport is not None:
                self._transport.close()
        except Exception:
            pass
        self._transport = None
        self.connect()

    # ------------------------------------------------------------------
    # REPL / raw REPL — delegated to the transport
    # ------------------------------------------------------------------

    def interrupt(self) -> None:
        """Send Ctrl-C and let the device drain whatever it was printing."""
        with self._lock:
            transport = self._ensure_transport()
            transport.serial.write(b'\r\x03')
            # Drain briefly so any traceback the program was printing
            # doesn't pollute the next exchange.
            deadline = time.monotonic() + 0.5
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if transport.serial.in_waiting:
                    transport.serial.read(transport.serial.in_waiting)
                    last_data = time.monotonic()
                elif time.monotonic() - last_data >= 0.1:
                    break
                else:
                    time.sleep(0.005)
            transport.in_raw_repl = False

    def enter_raw_repl(self) -> None:
        """Idempotent raw REPL entry, no soft reset."""
        with self._lock:
            transport = self._ensure_transport()
            if transport.in_raw_repl:
                return
            try:
                transport.enter_raw_repl(soft_reset=False)
            except TransportError as e:
                raise RuntimeError(f"Failed to enter raw REPL: {e}") from e

    def exit_raw_repl(self) -> None:
        with self._lock:
            if self._transport is None:
                return
            if not self._transport.in_raw_repl:
                return
            try:
                self._transport.exit_raw_repl()
            except Exception:
                # Force the flag back so we don't loop. Exit should never
                # be the thing that wedges us.
                self._transport.in_raw_repl = False

    @contextmanager
    def raw_repl_session(self) -> Generator[None, None, None]:
        """Keep raw REPL open across many executes.

        Nesting is safe: only the outermost call enters/exits.
        """
        with self._lock:
            transport = self._ensure_transport()
            owns = not transport.in_raw_repl
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
            transport = self._ensure_transport()
            # mpremote's enter_raw_repl(soft_reset=True) drives the full
            # soft-reset handshake (interrupt → raw REPL banner → Ctrl-D
            # → "soft reboot" marker → raw REPL banner). After that we
            # exit raw REPL so the device is in normal REPL mode, which
            # matches the previous behavior.
            try:
                transport.enter_raw_repl(soft_reset=True)
            except TransportError as e:
                raise RuntimeError(f"Soft reset failed: {e}") from e
            try:
                transport.exit_raw_repl()
            except Exception:
                transport.in_raw_repl = False
            # Drain any post-reset output (boot banner, etc.) so the next
            # call sees a quiescent port.
            buf = bytearray()
            deadline = time.monotonic() + 1.0
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if transport.serial.in_waiting:
                    buf.extend(transport.serial.read(transport.serial.in_waiting))
                    last_data = time.monotonic()
                elif time.monotonic() - last_data >= 0.1:
                    break
                else:
                    time.sleep(0.01)
            return buf.decode('utf-8', errors='replace')

    # ------------------------------------------------------------------
    # Execute — delegated to transport.exec_raw
    # ------------------------------------------------------------------

    def execute_raw(self, code: str, timeout: float = 10.0) -> tuple[str, str]:
        """Execute Python code, return (stdout, stderr)."""
        with self._lock:
            with self.raw_repl_session():
                transport = self._ensure_transport()
                for attempt in range(self.RECONNECT_RETRIES + 1):
                    try:
                        stdout, stderr = transport.exec_raw(
                            code.encode('utf-8'), timeout=timeout
                        )
                        return (
                            stdout.decode('utf-8', errors='replace'),
                            stderr.decode('utf-8', errors='replace'),
                        )
                    except serial.SerialException:
                        if attempt >= self.RECONNECT_RETRIES:
                            raise
                        time.sleep(self.RECONNECT_BACKOFF)
                        self._reopen()
                        # New transport, new raw REPL session.
                        self.enter_raw_repl()
                    except TransportError as e:
                        raise RuntimeError(f"Execution error: {e}") from e
                raise RuntimeError("unreachable")

    def execute(self, code: str, timeout: float = 10.0) -> str:
        """Execute and return stdout; raise RuntimeError if stderr is non-empty."""
        stdout, stderr = self.execute_raw(code, timeout)
        if stderr:
            raise RuntimeError(f"Execution error: {stderr.strip()}")
        return stdout

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

    # ------------------------------------------------------------------
    # Transport access (for filesystem ops that call transport.fs_*)
    # ------------------------------------------------------------------

    @property
    def transport(self) -> SerialTransport:
        """Underlying mpremote transport. Callers must hold the device
        lock if they're driving raw-REPL-affecting methods (fs_*, exec_*,
        enter/exit_raw_repl)."""
        return self._ensure_transport()


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
