"""Device program execution and output streaming for MicroPython devices."""

import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable
from queue import Queue, Empty

from .serial_connection import MicroPythonDevice


class RunState(Enum):
    """State of the device program execution."""
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ExecutionResult:
    """Result of a program execution."""
    output: str
    error: str | None = None
    return_value: str | None = None
    duration_ms: int = 0


@dataclass
class StreamingSession:
    """Represents an active streaming session with the device."""
    state: RunState = RunState.IDLE
    output_buffer: list[str] = field(default_factory=list)
    error_buffer: list[str] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _output_queue: Queue = field(default_factory=Queue)


class DeviceRunner:
    """Execute programs on MicroPython devices with output streaming."""

    def __init__(self, device: MicroPythonDevice):
        self.device = device
        self._session: StreamingSession | None = None
        self._reader_thread: threading.Thread | None = None

    def execute_code(self, code: str, timeout: float = 30.0) -> ExecutionResult:
        """
        Execute Python code on the device and return the result.

        Args:
            code: Python code to execute
            timeout: Maximum execution time in seconds

        Returns:
            ExecutionResult with output and any errors
        """
        start_time = time.time()

        try:
            stdout, stderr = self.device.execute_raw(code, timeout=timeout)
            duration = int((time.time() - start_time) * 1000)

            return ExecutionResult(
                output=stdout,
                error=stderr if stderr else None,
                duration_ms=duration
            )
        except Exception as e:
            duration = int((time.time() - start_time) * 1000)
            return ExecutionResult(
                output="",
                error=str(e),
                duration_ms=duration
            )

    def execute_file(self, file_path: str, timeout: float = 30.0) -> ExecutionResult:
        """
        Execute a Python file on the device.

        Args:
            file_path: Path to the file on the device
            timeout: Maximum execution time in seconds

        Returns:
            ExecutionResult with output and any errors
        """
        code = f'exec(open("{file_path}").read())'
        return self.execute_code(code, timeout)

    def run_main(self, timeout: float = 30.0) -> ExecutionResult:
        """
        Run the main.py file on the device.

        Returns:
            ExecutionResult with output and any errors
        """
        return self.execute_file("/main.py", timeout)

    def start_streaming(
        self,
        code: str | None = None,
        on_output: Callable[[str], None] | None = None
    ) -> StreamingSession:
        """
        Start streaming execution of code or REPL interaction.

        Args:
            code: Optional code to execute (if None, just opens REPL stream)
            on_output: Optional callback for each line of output

        Returns:
            StreamingSession object to track the session
        """
        if self._session and self._session.state == RunState.RUNNING:
            raise RuntimeError("A streaming session is already running")

        self._session = StreamingSession()
        self._session.state = RunState.RUNNING

        # Start reader thread
        self._reader_thread = threading.Thread(
            target=self._read_output_loop,
            args=(on_output,),
            daemon=True
        )
        self._reader_thread.start()

        # If code provided, send it
        if code:
            self.device.interrupt()
            time.sleep(0.1)
            # Send each line
            for line in code.split('\n'):
                self.device.send_line(line)

        return self._session

    def _read_output_loop(self, on_output: Callable[[str], None] | None) -> None:
        """Background thread to read device output."""
        if not self._session:
            return

        line_buffer = ""

        while not self._session._stop_event.is_set():
            try:
                data = self.device.read_available()
                if data:
                    text = data.decode('utf-8', errors='replace')
                    line_buffer += text

                    # Process complete lines
                    while '\n' in line_buffer:
                        line, line_buffer = line_buffer.split('\n', 1)
                        line = line.rstrip('\r')

                        self._session.output_buffer.append(line)
                        self._session._output_queue.put(line)

                        if on_output:
                            on_output(line)
                else:
                    time.sleep(0.01)

            except Exception as e:
                self._session.error_buffer.append(str(e))
                self._session.state = RunState.ERROR
                break

        self._session.state = RunState.STOPPED

    def stop_streaming(self) -> None:
        """Stop the current streaming session."""
        if self._session:
            self._session._stop_event.set()
            self.device.interrupt()

            if self._reader_thread:
                self._reader_thread.join(timeout=2.0)
                self._reader_thread = None

            self._session.state = RunState.STOPPED

    def get_output(self, timeout: float = 0.1) -> str | None:
        """
        Get the next line of output from the streaming session.

        Args:
            timeout: How long to wait for output

        Returns:
            Next line of output or None if no output available
        """
        if not self._session:
            return None

        try:
            return self._session._output_queue.get(timeout=timeout)
        except Empty:
            return None

    def get_all_output(self) -> list[str]:
        """Get all buffered output from the streaming session."""
        if not self._session:
            return []
        return list(self._session.output_buffer)

    def send_input(self, text: str) -> None:
        """
        Send input to the running program/REPL.

        Args:
            text: Text to send (will have newline appended)
        """
        self.device.send_line(text)

    def send_interrupt(self) -> None:
        """Send Ctrl+C interrupt to the device."""
        self.device.interrupt()

    def soft_reset(self) -> str:
        """Perform a soft reset of the device."""
        self.stop_streaming()
        return self.device.soft_reset()

    def is_running(self) -> bool:
        """Check if a streaming session is currently running."""
        return self._session is not None and self._session.state == RunState.RUNNING


class InteractiveSession:
    """
    Manages an interactive REPL session with command history and state.
    """

    def __init__(self, device: MicroPythonDevice):
        self.device = device
        self.runner = DeviceRunner(device)
        self.command_history: list[str] = []
        self.output_history: list[tuple[str, str]] = []  # (command, output)

    def execute(self, command: str, timeout: float = 10.0) -> str:
        """
        Execute a command and return the output.

        Args:
            command: Python code to execute
            timeout: Maximum execution time

        Returns:
            Output from the command
        """
        result = self.runner.execute_code(command, timeout)

        self.command_history.append(command)

        output = result.output
        if result.error:
            output += f"\nError: {result.error}"

        self.output_history.append((command, output))

        return output

    def run_script(self, script: str, timeout: float = 30.0) -> str:
        """
        Execute a multi-line script.

        Args:
            script: Multi-line Python script
            timeout: Maximum execution time

        Returns:
            Output from the script
        """
        return self.execute(script, timeout)

    def get_variable(self, name: str) -> str:
        """Get the value of a variable on the device."""
        if not name.isidentifier():
            raise ValueError(f"Invalid variable name: {name!r}")
        return self.execute(f"print(repr({name}))")

    def set_variable(self, name: str, value: str) -> str:
        """Set a variable on the device."""
        if not name.isidentifier():
            raise ValueError(f"Invalid variable name: {name!r}")
        return self.execute(f"{name} = {value}")

    def import_module(self, module: str) -> str:
        """Import a module on the device."""
        if not all(part.isidentifier() for part in module.split('.')):
            raise ValueError(f"Invalid module name: {module!r}")
        return self.execute(f"import {module}")

    def reset(self) -> str:
        """Reset the device and clear session state."""
        output = self.runner.soft_reset()
        self.command_history.clear()
        self.output_history.clear()
        return output

    def get_history(self, limit: int = 10) -> list[tuple[str, str]]:
        """Get recent command history with outputs."""
        return self.output_history[-limit:]
