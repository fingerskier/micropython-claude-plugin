"""Unit tests for disconnect / reconnect / serial-flake recovery (TODO #10).

These tests pin the contracts that protect against the most common
serial-transport failures:

  * ``disconnect`` while a raw-REPL session is open exits raw REPL
    cleanly (no leaked terminal, ``_transport`` is reset to None).
  * ``disconnect`` is idempotent — calling it twice or on a never-
    connected device must not raise.
  * ``execute_raw`` retries once after a ``serial.SerialException``,
    re-opens the transport, and returns the post-retry result. This
    is the common USB-CDC enumeration glitch we see on the Pico W
    on COM11 (Reqall #2141).
  * After exhausting ``RECONNECT_RETRIES``, ``execute_raw`` propagates
    the SerialException — silent failure would mask hardware issues.
  * ``mpremote.transport.TransportError`` is converted to a
    ``RuntimeError`` (not retried — these are protocol errors, not
    transport drops, and retrying would loop on a broken device).

The hardware case (physical USB yank during a large ``write_file``,
reconnect, retry) is necessarily out-of-scope for an unattended unit
test — captured separately as a manual test plan in TODO.md.

Implementation: monkey-patch ``serial_connection.SerialTransport`` with
a configurable fake. The fake records every ``exec_raw`` call and
serves a programmable script of side-effects (raise SerialException
once, then return; or raise unconditionally; etc.) so each test
exercises one specific recovery branch.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import serial

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mpremote.transport import TransportError

from micropython_claude_plugin import serial_connection as sc
from micropython_claude_plugin.serial_connection import MicroPythonDevice


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class _FakeSerial:
    """Minimal stand-in for the underlying pyserial handle. We only
    need the attrs the recovery code touches (``is_open``, ``timeout``,
    ``in_waiting``, ``read``, ``reset_*_buffer``)."""

    def __init__(self):
        self.is_open = True
        self.timeout = None
        self.in_waiting = 0
        self._closed = False

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def read(self, n):
        return b""

    def write(self, data):
        return len(data)

    def close(self):
        self._closed = True
        self.is_open = False


class FakeTransport:
    """Configurable stand-in for ``mpremote.SerialTransport``.

    Tests pre-program ``exec_raw_script`` as a list of effects (each
    either a ``(stdout, stderr)`` tuple or an Exception class/instance
    to raise). ``exec_raw`` pops the next effect on every call.
    """

    # Class-level registry of construction calls so a test can count
    # how many times ``connect`` rebuilt the transport. Reset in fixture.
    construction_log: list[str] = []

    def __init__(self, port, baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self.serial = _FakeSerial()
        self.in_raw_repl = False
        self.exec_raw_calls: list[bytes] = []
        self.exec_raw_script: list = []
        self.exit_raw_calls = 0
        self.enter_raw_calls = 0
        self.closed = False
        FakeTransport.construction_log.append(port)

    def enter_raw_repl(self, soft_reset=False):
        self.enter_raw_calls += 1
        self.in_raw_repl = True

    def exit_raw_repl(self):
        self.exit_raw_calls += 1
        self.in_raw_repl = False

    def exec_raw(self, code: bytes, timeout: float = 10.0):
        self.exec_raw_calls.append(code)
        if not self.exec_raw_script:
            return (b"OK\n", b"")
        effect = self.exec_raw_script.pop(0)
        if isinstance(effect, type) and issubclass(effect, BaseException):
            raise effect("scripted")
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def close(self):
        self.closed = True
        self.serial.close()


@pytest.fixture
def patched_transport(monkeypatch):
    """Replace ``serial_connection.SerialTransport`` with FakeTransport
    for the test, and clear the construction log so each test starts
    clean. Returns a function that connects a device and returns it
    along with its transport (for ergonomic access)."""
    FakeTransport.construction_log = []
    monkeypatch.setattr(sc, "SerialTransport", FakeTransport)

    def _connect(port: str = "FAKE0") -> tuple[MicroPythonDevice, FakeTransport]:
        d = MicroPythonDevice(port)
        d.connect()
        # The transport is whatever connect() built. After _reopen() the
        # current handle changes, so callers should re-read d._transport.
        return d, d._transport  # type: ignore[return-value]

    return _connect


# ---------------------------------------------------------------------------
# Disconnect contracts
# ---------------------------------------------------------------------------


class TestDisconnect:
    """``disconnect`` must always leave the device in a clean state —
    no leaked raw-REPL terminal, no half-open transport. Important
    because callers (server.py:disconnect, /loop teardown) call this
    on potentially-broken devices."""

    def test_disconnect_during_raw_repl_exits_cleanly(self, patched_transport):
        d, t = patched_transport()
        d.enter_raw_repl()
        assert t.in_raw_repl is True

        d.disconnect()

        assert t.exit_raw_calls == 1, (
            "disconnect must call exit_raw_repl while a session was open"
        )
        assert t.closed is True
        assert d._transport is None
        assert d.is_connected is False

    def test_disconnect_when_not_in_raw_repl_does_not_call_exit(self, patched_transport):
        d, t = patched_transport()
        # Never entered raw REPL.
        d.disconnect()
        assert t.exit_raw_calls == 0
        assert t.closed is True
        assert d._transport is None

    def test_disconnect_idempotent_on_never_connected(self):
        """A device that was never connected must not raise on disconnect."""
        d = MicroPythonDevice("FAKE0")
        d.disconnect()  # no-op, no exception
        assert d._transport is None

    def test_disconnect_idempotent_called_twice(self, patched_transport):
        d, _ = patched_transport()
        d.disconnect()
        d.disconnect()  # second call must be a no-op, not a crash
        assert d._transport is None

    def test_disconnect_swallows_exit_raw_failure(self, patched_transport, monkeypatch):
        """If exit_raw_repl raises during teardown (e.g. device is
        already gone), disconnect must still close the transport and
        clear ``_transport``. Otherwise the lock could be wedged
        forever on a leaked transport handle."""
        d, t = patched_transport()
        d.enter_raw_repl()

        def _raising(*a, **kw):
            raise TransportError("device gone")

        monkeypatch.setattr(t, "exit_raw_repl", _raising)

        d.disconnect()  # must not raise
        assert d._transport is None


# ---------------------------------------------------------------------------
# execute_raw retry / recovery
# ---------------------------------------------------------------------------


class TestExecuteRawSerialFlakeRetry:
    """execute_raw retries once on SerialException by re-opening the
    transport. RECONNECT_RETRIES=1, RECONNECT_BACKOFF=0.5 — but the
    fake's exec_raw is fast, so total test time is bounded by the
    sleep, not the I/O."""

    def test_retry_succeeds_after_one_serial_exception(self, patched_transport, monkeypatch):
        """First exec_raw raises serial.SerialException → device
        reopens (new transport built) → second attempt returns OK.
        The test asserts both that the result is correct AND that
        the transport was rebuilt (construction_log grows)."""
        # Speed up the test — the default backoff is 0.5s, fine but
        # avoidable for a CI test.
        monkeypatch.setattr(MicroPythonDevice, "RECONNECT_BACKOFF", 0.0)

        d, t = patched_transport()
        # First call: raise SerialException. Subsequent calls: succeed.
        # NOTE: after _reopen, ``d._transport`` is a NEW FakeTransport
        # whose script is empty by default → succeeds with default.
        t.exec_raw_script = [serial.SerialException]

        stdout, stderr = d.execute_raw("print(1)")

        assert stdout == "OK\n", f"Expected post-retry stdout, got: {stdout!r}"
        assert stderr == ""
        assert len(FakeTransport.construction_log) == 2, (
            f"_reopen must rebuild transport once; construction_log={FakeTransport.construction_log}"
        )

    def test_exhausted_retries_propagates_serial_exception(self, patched_transport, monkeypatch):
        """If every attempt raises SerialException, the final attempt
        re-raises rather than silently returning. RECONNECT_RETRIES=1
        means we get 2 total attempts (initial + 1 retry)."""
        monkeypatch.setattr(MicroPythonDevice, "RECONNECT_BACKOFF", 0.0)

        # Pre-pollute *every* future transport construction with a
        # raising exec_raw_script. We do this by replacing FakeTransport
        # with a subclass that auto-stocks the script on __init__.
        class AlwaysFlaky(FakeTransport):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.exec_raw_script = [serial.SerialException]

        monkeypatch.setattr(sc, "SerialTransport", AlwaysFlaky)
        FakeTransport.construction_log = []

        d = MicroPythonDevice("FAKE0")
        d.connect()

        with pytest.raises(serial.SerialException):
            d.execute_raw("print(1)")

        # connect() built one transport; _reopen built one more. The
        # second exec_raw also raised, so we re-raise rather than
        # building a third — RECONNECT_RETRIES=1 caps total attempts.
        assert len(FakeTransport.construction_log) == 2, (
            f"Expected 2 transports (initial + 1 retry); got "
            f"{len(FakeTransport.construction_log)}"
        )

    def test_transport_error_not_retried(self, patched_transport, monkeypatch):
        """TransportError is a protocol-level error (e.g. unexpected
        bytes in raw REPL framing), not a transport drop. Retrying
        would loop on a broken-but-connected device. Must convert to
        RuntimeError on the first attempt without rebuilding the
        transport."""
        monkeypatch.setattr(MicroPythonDevice, "RECONNECT_BACKOFF", 0.0)

        d, t = patched_transport()
        t.exec_raw_script = [TransportError]

        with pytest.raises(RuntimeError, match="Execution error"):
            d.execute_raw("print(1)")

        # Only the original transport — no _reopen happened.
        assert len(FakeTransport.construction_log) == 1, (
            f"TransportError must NOT trigger _reopen; "
            f"construction_log={FakeTransport.construction_log}"
        )


# ---------------------------------------------------------------------------
# Lock release after error
# ---------------------------------------------------------------------------


class TestLockReleaseAfterError:
    """An exception inside a ``with self._lock:`` block must release
    the lock so subsequent calls don't deadlock. RLock specifically
    permits the same thread to re-acquire — but a leaked lock from a
    leaked thread would still wedge a server that uses asyncio across
    handlers."""

    def test_lock_released_after_execute_raises(self, patched_transport, monkeypatch):
        """After execute_raw raises (TransportError → RuntimeError),
        we can immediately call execute_raw again on the same device
        without deadlocking. Catches a regression where the ``with
        self._lock`` exit was somehow skipped."""
        monkeypatch.setattr(MicroPythonDevice, "RECONNECT_BACKOFF", 0.0)
        d, t = patched_transport()
        # First call raises; second succeeds via default empty script.
        t.exec_raw_script = [TransportError]

        with pytest.raises(RuntimeError):
            d.execute_raw("boom")

        # If the lock leaked, this second call would block forever.
        # We protect ourselves with a thread + timeout below in case
        # it does — pytest's default test timeout would still catch
        # it but the failure mode would be confusing.
        import threading
        result_box: dict = {}

        def _try():
            try:
                result_box["out"] = d.execute_raw("print('after')")
            except BaseException as e:
                result_box["err"] = e

        th = threading.Thread(target=_try)
        th.start()
        th.join(timeout=5.0)
        assert not th.is_alive(), "Second execute_raw deadlocked — lock leaked"
        assert "out" in result_box, f"Second execute raised: {result_box.get('err')!r}"

    def test_lock_released_after_disconnect(self, patched_transport):
        """disconnect must release the lock even if the underlying
        transport.close() raises. Otherwise a half-broken device
        wedges the whole MCP server."""
        d, t = patched_transport()

        # Make close() raise — disconnect's try/except should swallow
        # it and still release the lock.
        def _raising_close():
            raise OSError("port already gone")
        t.close = _raising_close  # type: ignore[method-assign]

        d.disconnect()
        # Lock release is implicit in ``with`` — verify by re-acquiring.
        # A leaked RLock would hang here forever on a different thread.
        import threading
        acquired = threading.Event()

        def _check():
            with d._lock:
                acquired.set()

        th = threading.Thread(target=_check)
        th.start()
        th.join(timeout=2.0)
        assert acquired.is_set(), "Lock leaked across disconnect → close() failure"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
