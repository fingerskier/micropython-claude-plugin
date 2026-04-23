"""Device program execution and output streaming for MicroPython devices.

Two modes:

* ``DeviceRunner.execute_code / execute_file / run_main`` — blocking,
  raw-REPL backed. Good for short scripts that return output quickly.

* ``DeviceRunner.start_program / read_output / send_input / stop_program``
  — streaming, raw-REPL exited and the device is driven in normal REPL
  mode with a background reader thread. Good for programs that run for
  a long time, print continuously, or accept input.

``InteractiveSession`` keeps a raw REPL open across its lifetime so a
chain of ``execute()`` calls shares one enter/exit rather than paying
that cost per call.
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from queue import Empty, Queue
from typing import Callable

from .file_ops import _sanitize_path
from .serial_connection import MicroPythonDevice


class RunState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ExecutionResult:
    output: str
    error: str | None = None
    return_value: str | None = None
    duration_ms: int = 0


@dataclass
class StreamingSession:
    state: RunState = RunState.IDLE
    output_buffer: list[str] = field(default_factory=list)
    error_buffer: list[str] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _output_queue: Queue = field(default_factory=Queue)


class DeviceRunner:
    """Execute programs on MicroPython devices, blocking or streaming."""

    def __init__(self, device: MicroPythonDevice):
        self.device = device
        self._session: StreamingSession | None = None
        self._reader_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Blocking execution
    # ------------------------------------------------------------------

    def execute_code(self, code: str, timeout: float = 30.0) -> ExecutionResult:
        start_time = time.time()
        try:
            stdout, stderr = self.device.execute_raw(code, timeout=timeout)
            return ExecutionResult(
                output=stdout,
                error=stderr if stderr else None,
                duration_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            return ExecutionResult(
                output="",
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000),
            )

    def execute_file(self, file_path: str, timeout: float = 30.0) -> ExecutionResult:
        # Sanitize — the path is interpolated into Python source run on device.
        file_path = _sanitize_path(file_path)
        code = f'exec(open("{file_path}").read())'
        return self.execute_code(code, timeout)

    def run_main(self, timeout: float = 30.0) -> ExecutionResult:
        return self.execute_file("/main.py", timeout)

    # ------------------------------------------------------------------
    # Streaming execution (long-running programs)
    # ------------------------------------------------------------------

    def start_program(
        self,
        code: str | None = None,
        file_path: str | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> StreamingSession:
        """Start a long-running program and stream output.

        Exactly one of ``code`` or ``file_path`` should be provided. If
        neither is given, the current program is not modified — the
        reader thread just starts capturing whatever the device emits
        (useful for attaching to a device that's already running).

        While a streaming session is active, do not call ``execute_code``
        on the same device: the reader thread races for serial bytes.
        """
        if self._session and self._session.state == RunState.RUNNING:
            raise RuntimeError("A streaming session is already running")

        if code is not None and file_path is not None:
            raise ValueError("Pass either code or file_path, not both")

        # Drop raw REPL so normal REPL echo / program output is what we see.
        self.device.exit_raw_repl()

        if code is not None:
            self.device.interrupt()
            time.sleep(0.05)
            for line in code.split('\n'):
                self.device.send_line(line)
        elif file_path is not None:
            file_path = _sanitize_path(file_path)
            self.device.interrupt()
            time.sleep(0.05)
            self.device.send_line(f'exec(open("{file_path}").read())')

        self._session = StreamingSession()
        self._session.state = RunState.RUNNING
        self._reader_thread = threading.Thread(
            target=self._read_output_loop,
            args=(on_output,),
            daemon=True,
        )
        self._reader_thread.start()
        return self._session

    # Backwards-compat alias.
    start_streaming = start_program

    def _read_output_loop(self, on_output: Callable[[str], None] | None) -> None:
        if not self._session:
            return
        line_buffer = ""
        while not self._session._stop_event.is_set():
            try:
                data = self.device.read_available()
                if data:
                    line_buffer += data.decode('utf-8', errors='replace')
                    while '\n' in line_buffer:
                        line, line_buffer = line_buffer.split('\n', 1)
                        line = line.rstrip('\r')
                        self._session.output_buffer.append(line)
                        self._session._output_queue.put(line)
                        if on_output:
                            try:
                                on_output(line)
                            except Exception:
                                pass
                else:
                    time.sleep(0.01)
            except Exception as e:
                self._session.error_buffer.append(str(e))
                self._session.state = RunState.ERROR
                break
        # Flush any trailing partial line.
        if line_buffer.strip():
            self._session.output_buffer.append(line_buffer.rstrip('\r'))
            self._session._output_queue.put(line_buffer.rstrip('\r'))
        if self._session.state != RunState.ERROR:
            self._session.state = RunState.STOPPED

    def stop_program(self) -> None:
        if not self._session:
            return
        self._session._stop_event.set()
        try:
            self.device.interrupt()
        except Exception:
            pass
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        self._session.state = RunState.STOPPED

    # Backwards-compat alias.
    stop_streaming = stop_program

    def get_output(self, timeout: float = 0.1) -> str | None:
        if not self._session:
            return None
        try:
            return self._session._output_queue.get(timeout=timeout)
        except Empty:
            return None

    def read_output(self, max_lines: int = 200, wait: float = 0.1) -> list[str]:
        """Drain up to ``max_lines`` of output, waiting up to ``wait`` seconds
        for the first line. Returns [] if nothing is available.
        """
        if not self._session:
            return []
        lines: list[str] = []
        try:
            first = self._session._output_queue.get(timeout=wait)
            lines.append(first)
        except Empty:
            return lines
        while len(lines) < max_lines:
            try:
                lines.append(self._session._output_queue.get_nowait())
            except Empty:
                break
        return lines

    def get_all_output(self) -> list[str]:
        if not self._session:
            return []
        return list(self._session.output_buffer)

    def send_input(self, text: str) -> None:
        self.device.send_line(text)

    def send_interrupt(self) -> None:
        self.device.interrupt()

    def soft_reset(self) -> str:
        self.stop_program()
        return self.device.soft_reset()

    def is_running(self) -> bool:
        return self._session is not None and self._session.state == RunState.RUNNING


class InteractiveSession:
    """A command log that keeps raw REPL open for its lifetime.

    Entering the context (or calling ``open()``) enters raw REPL once;
    ``close()`` exits. While open, ``execute()`` calls reuse that single
    raw REPL, which makes chained operations (e.g., get_variable after
    set_variable) materially faster.

    ``set_variable`` **interpolates its value string directly into the
    code executed on device**. That's by design — the value argument is
    a Python expression, same as the REPL. Callers must only pass trusted
    expressions.
    """

    def __init__(self, device: MicroPythonDevice):
        self.device = device
        self.runner = DeviceRunner(device)
        self.command_history: list[str] = []
        self.output_history: list[tuple[str, str]] = []
        self._ctx = None

    def open(self) -> None:
        if self._ctx is None:
            self._ctx = self.device.raw_repl_session()
            self._ctx.__enter__()

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.__exit__(None, None, None)
            finally:
                self._ctx = None

    def __enter__(self) -> "InteractiveSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def execute(self, command: str, timeout: float = 10.0) -> str:
        # Wrap in a raw REPL session so one-off uses still work; if we're
        # already inside the session (via open()/__enter__), the context
        # manager is idempotent.
        with self.device.raw_repl_session():
            result = self.runner.execute_code(command, timeout)

        self.command_history.append(command)
        output = result.output
        if result.error:
            output += f"\nError: {result.error}"
        self.output_history.append((command, output))
        return output

    def run_script(self, script: str, timeout: float = 30.0) -> str:
        return self.execute(script, timeout)

    def get_variable(self, name: str) -> str:
        if not name.isidentifier():
            raise ValueError(f"Invalid variable name: {name!r}")
        return self.execute(f"print(repr({name}))")

    def set_variable(self, name: str, value: str) -> str:
        """Assign ``value`` to ``name`` on device.

        WARNING: ``value`` is interpolated into Python source run on the
        device. This is a code-execution path by contract; callers must
        vet ``value`` themselves.
        """
        if not name.isidentifier():
            raise ValueError(f"Invalid variable name: {name!r}")
        return self.execute(f"{name} = {value}")

    def import_module(self, module: str) -> str:
        if not all(part.isidentifier() for part in module.split('.')):
            raise ValueError(f"Invalid module name: {module!r}")
        return self.execute(f"import {module}")

    def reset(self) -> str:
        output = self.runner.soft_reset()
        self.command_history.clear()
        self.output_history.clear()
        # Soft-reset leaves the device in normal REPL; drop any cached
        # raw-REPL session we were holding.
        self._ctx = None
        return output

    def get_history(self, limit: int = 10) -> list[tuple[str, str]]:
        return self.output_history[-limit:]
