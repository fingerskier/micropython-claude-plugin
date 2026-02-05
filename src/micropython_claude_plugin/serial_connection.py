"""Serial connection manager for MicroPython devices."""

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import serial
import serial.tools.list_ports


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

    # MicroPython REPL control characters
    CTRL_A = b'\x01'  # Enter raw REPL
    CTRL_B = b'\x02'  # Exit raw REPL
    CTRL_C = b'\x03'  # Interrupt
    CTRL_D = b'\x04'  # Soft reset / execute in raw REPL

    RAW_REPL_PROMPT = b'>'
    NORMAL_REPL_PROMPT = b'>>> '

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 1.0
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: serial.Serial | None = None

    @property
    def is_connected(self) -> bool:
        """Check if device is connected."""
        return self._serial is not None and self._serial.is_open

    def connect(self) -> None:
        """Connect to the device."""
        if self.is_connected:
            return

        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout
        )
        # Give device time to initialize
        time.sleep(0.1)
        # Clear any pending data
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def disconnect(self) -> None:
        """Disconnect from the device."""
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def _ensure_connected(self) -> serial.Serial:
        """Ensure we have a valid connection."""
        if not self.is_connected or self._serial is None:
            raise ConnectionError("Not connected to device")
        return self._serial

    def interrupt(self) -> None:
        """Send interrupt signal to device (Ctrl+C)."""
        ser = self._ensure_connected()
        ser.write(self.CTRL_C)
        time.sleep(0.1)
        ser.reset_input_buffer()

    def enter_raw_repl(self) -> None:
        """Enter raw REPL mode for programmatic interaction."""
        ser = self._ensure_connected()

        # Interrupt any running program
        ser.write(self.CTRL_C)
        time.sleep(0.1)
        ser.write(self.CTRL_C)
        time.sleep(0.1)

        # Clear buffer
        ser.reset_input_buffer()

        # Enter raw REPL
        ser.write(self.CTRL_A)
        time.sleep(0.1)

        # Read until we get the raw REPL prompt
        response = self._read_until(b'raw REPL; CTRL-B to exit\r\n>', timeout=2.0)
        if b'raw REPL' not in response:
            raise RuntimeError("Failed to enter raw REPL mode")

    def exit_raw_repl(self) -> None:
        """Exit raw REPL mode and return to normal REPL."""
        ser = self._ensure_connected()
        ser.write(self.CTRL_B)
        time.sleep(0.1)
        ser.reset_input_buffer()

    def soft_reset(self) -> str:
        """Perform a soft reset of the device."""
        ser = self._ensure_connected()

        # Interrupt and enter raw REPL
        self.interrupt()
        self.enter_raw_repl()

        # Send Ctrl+D to soft reset
        ser.write(self.CTRL_D)
        time.sleep(0.5)

        # Read the reset output
        output = self._read_until(self.NORMAL_REPL_PROMPT, timeout=5.0)
        return output.decode('utf-8', errors='replace')

    def execute_raw(self, code: str, timeout: float = 10.0) -> tuple[str, str]:
        """
        Execute Python code in raw REPL mode.

        Returns:
            Tuple of (stdout, stderr)
        """
        ser = self._ensure_connected()

        # Enter raw REPL
        self.enter_raw_repl()

        try:
            # Send the code
            code_bytes = code.encode('utf-8')
            ser.write(code_bytes)

            # Execute with Ctrl+D
            ser.write(self.CTRL_D)

            # Read response - format is: OK<stdout>\x04<stderr>\x04>
            response = self._read_until(b'\x04>', timeout=timeout)

            # Parse response
            if response.startswith(b'OK'):
                response = response[2:]  # Remove 'OK'

            # Split stdout and stderr
            parts = response.rstrip(b'\x04>').split(b'\x04')
            stdout = parts[0].decode('utf-8', errors='replace') if parts else ''
            stderr = parts[1].decode('utf-8', errors='replace') if len(parts) > 1 else ''

            return stdout, stderr

        finally:
            self.exit_raw_repl()

    def execute(self, code: str, timeout: float = 10.0) -> str:
        """
        Execute Python code and return combined output.

        Raises RuntimeError if there's an error.
        """
        stdout, stderr = self.execute_raw(code, timeout)
        if stderr:
            raise RuntimeError(f"Execution error: {stderr}")
        return stdout

    def _read_until(self, terminator: bytes, timeout: float = 5.0) -> bytes:
        """Read from serial until terminator is found or timeout."""
        ser = self._ensure_connected()

        start_time = time.time()
        data = b''

        while time.time() - start_time < timeout:
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting)
                data += chunk
                if terminator in data:
                    return data
            else:
                time.sleep(0.01)

        return data

    def read_available(self) -> bytes:
        """Read all available data from serial buffer."""
        ser = self._ensure_connected()
        if ser.in_waiting:
            return ser.read(ser.in_waiting)
        return b''

    def write(self, data: bytes) -> int:
        """Write data to the serial port."""
        ser = self._ensure_connected()
        return ser.write(data)

    def send_line(self, line: str) -> None:
        """Send a line of text to the device REPL."""
        ser = self._ensure_connected()
        ser.write((line + '\r\n').encode('utf-8'))


@contextmanager
def device_connection(
    port: str,
    baudrate: int = 115200,
    timeout: float = 1.0
) -> Generator[MicroPythonDevice, None, None]:
    """Context manager for device connections."""
    device = MicroPythonDevice(port, baudrate, timeout)
    device.connect()
    try:
        yield device
    finally:
        device.disconnect()


def list_devices() -> list[DeviceInfo]:
    """List available serial ports that might be MicroPython devices."""
    devices = []

    for port_info in serial.tools.list_ports.comports():
        # Include all serial ports - user can filter
        devices.append(DeviceInfo(
            port=port_info.device,
            description=port_info.description,
            hwid=port_info.hwid,
            vid=port_info.vid,
            pid=port_info.pid
        ))

    return devices


def find_micropython_devices() -> list[DeviceInfo]:
    """Find likely MicroPython devices based on common VID/PIDs."""
    # Common MicroPython device identifiers
    micropython_ids = [
        (0x2E8A, None),   # Raspberry Pi (Pico)
        (0x1A86, 0x7523), # CH340 (common on ESP boards)
        (0x10C4, 0xEA60), # CP210x (common on ESP boards)
        (0x0403, 0x6001), # FTDI (various boards)
        (0x303A, None),   # Espressif
    ]

    devices = []
    for device in list_devices():
        for vid, pid in micropython_ids:
            if device.vid == vid and (pid is None or device.pid == pid):
                devices.append(device)
                break

    return devices
