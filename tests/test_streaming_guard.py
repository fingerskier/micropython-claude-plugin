"""Unit tests for the streaming-guard reject path (TODO #9).

The MCP server at ``server.py:441`` rejects tools that drive raw-REPL
exchanges while a streaming program is running, to keep the background
reader thread from racing the protocol. Only ``_STREAMING_SAFE_TOOLS``
(connect, disconnect, read_output, send_input, stop_program, interrupt,
list_devices) are dispatched in that state. Without this guard, the
streaming reader and the raw-REPL exchange steal bytes from each other
and corrupt the serial stream.

These tests exercise the guard with a fake runner — no hardware, no
real MCP transport. They patch the module globals (``_runner``,
``_device``, ``_file_ops``, ``_image_ops``, ``_session``) so the guard
sees a "running" or "not running" state on demand, then call the
``call_tool`` handler directly. The guard runs before any device
interaction, so a runner whose ``is_running()`` is the only honest
attribute is enough.

End-to-end (real device, real ``start_program``/``stop_program``) is
covered by ``tests/test_streaming_guard_hw.py``.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin import server as srv
from micropython_claude_plugin.server import _STREAMING_SAFE_TOOLS


class _FakeRunner:
    """Minimal stand-in for DeviceRunner: only ``is_running()`` is honest."""

    def __init__(self, running: bool):
        self._running = running

    def is_running(self) -> bool:
        return self._running


@pytest.fixture(autouse=True)
def _isolate_server_state(monkeypatch):
    """Reset the global module state around every test so a leaked
    ``_runner`` from one case can't poison another."""
    monkeypatch.setattr(srv, "_runner", None, raising=False)
    monkeypatch.setattr(srv, "_session", None, raising=False)
    monkeypatch.setattr(srv, "_device", None, raising=False)
    monkeypatch.setattr(srv, "_file_ops", None, raising=False)
    monkeypatch.setattr(srv, "_image_ops", None, raising=False)
    yield


def _set_runner(monkeypatch, running: bool):
    monkeypatch.setattr(srv, "_runner", _FakeRunner(running), raising=False)


async def _call(name: str, arguments: dict) -> list:
    return await srv.call_tool(name, arguments)


def _text(result) -> str:
    """Extract the first TextContent body from a handler result."""
    return result[0].text


# ---------------------------------------------------------------------------
# Reject path: non-safe tools are blocked while the runner is running.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    sorted({
        # Sample of clearly raw-REPL-driving tools — everything in this
        # set drives transport.fs_* / transport.exec_raw under the hood.
        "list_files",
        "read_file",
        "write_file",
        "delete_file",
        "execute_code",
        "pull_image",
        "push_image",
    }),
)
def test_non_safe_tool_rejected_when_running(monkeypatch, tool_name):
    """Each non-safe tool returns the streaming-active error message
    rather than dispatching. The error names the tool that was rejected
    so a caller can route around it."""
    _set_runner(monkeypatch, running=True)
    result = asyncio.run(_call(tool_name, {}))
    body = _text(result)
    assert "streaming program is running" in body, body
    assert "would race the background reader thread" in body, body
    assert tool_name in body, (
        f"Expected error to name the rejected tool {tool_name!r}; got: {body!r}"
    )


# ---------------------------------------------------------------------------
# Allow path: safe tools (and various not-running cases) bypass the guard.
# ---------------------------------------------------------------------------


def test_safe_tool_dispatches_when_running(monkeypatch):
    """A streaming-safe tool ('list_devices' is the simplest — it does
    not need a connected device) is dispatched even while the runner is
    running. The guard must NOT short-circuit it."""
    _set_runner(monkeypatch, running=True)
    result = asyncio.run(_call("list_devices", {"filter_micropython": False}))
    body = _text(result)
    assert "streaming program is running" not in body, (
        f"Safe tool was incorrectly rejected: {body!r}"
    )
    # Real dispatch path runs serial.tools.list_ports and returns JSON;
    # we don't pin the content (depends on host hardware) — just that
    # we got past the guard into _dispatch.


def test_no_runner_means_no_guard(monkeypatch):
    """When ``_runner`` is None (no streaming session ever started), the
    guard short-circuit must never fire even for non-safe tools."""
    monkeypatch.setattr(srv, "_runner", None, raising=False)
    # list_files needs a device — without it we get a different error
    # ("No device connected"), but crucially NOT the streaming error.
    result = asyncio.run(_call("list_files", {"path": "/"}))
    body = _text(result)
    assert "streaming program is running" not in body, body


def test_runner_not_running_means_no_guard(monkeypatch):
    """A finished/stopped runner (``is_running()=False``) is the post-
    stop_program state. Non-safe tools must dispatch normally again."""
    _set_runner(monkeypatch, running=False)
    result = asyncio.run(_call("list_files", {"path": "/"}))
    body = _text(result)
    assert "streaming program is running" not in body, body


# ---------------------------------------------------------------------------
# Contract on the safe set — guard against accidental shrinkage.
# ---------------------------------------------------------------------------


def test_streaming_safe_tools_contains_recovery_path():
    """The safe set must include the tools a caller needs to escape the
    streaming state. If a refactor accidentally removes one, the user
    can no longer call stop_program/interrupt while a program runs and
    is wedged."""
    required = {"stop_program", "interrupt", "read_output", "send_input"}
    missing = required - _STREAMING_SAFE_TOOLS
    assert not missing, f"Streaming-safe set is missing recovery tools: {missing}"


def test_streaming_safe_tools_excludes_raw_repl_drivers():
    """A handful of tools that obviously drive raw-REPL exchanges must
    NOT be in the safe set, otherwise the reader thread races them."""
    forbidden = {
        "list_files", "read_file", "write_file", "delete_file",
        "execute_code", "pull_image", "push_image", "compare_image",
        "sync_file", "sync_directory",
    }
    bad = forbidden & _STREAMING_SAFE_TOOLS
    assert not bad, (
        f"Raw-REPL-driving tools accidentally allowed during streaming: {bad}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
