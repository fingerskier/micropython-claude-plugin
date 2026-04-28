"""Hardware end-to-end test for the streaming guard (TODO #9).

Drives a real device through the full streaming lifecycle:
  1. Connect via the MCP ``connect`` tool.
  2. ``start_program`` with an infinite-loop print.
  3. Verify ``read_output`` produces lines (proves the reader thread is
     attached and the program is alive).
  4. Call ``list_files`` via the same dispatch path. The guard at
     ``server.py:441`` must reject it with a message naming the tool
     and explaining the race risk — NOT actually drive an fs_listdir
     against the running stream (that would corrupt bytes).
  5. ``stop_program`` cleanly stops the reader thread and exits.
  6. After stop, ``list_files`` succeeds — proves the guard releases.

This catches a class of regressions where the guard fires correctly in
unit tests but the post-stop ``_runner.is_running()`` flag isn't reset
on real hardware (e.g. if the reader thread doesn't join cleanly).

Usage:
    python tests/test_streaming_guard_hw.py            # auto-detect single MP port
    python tests/test_streaming_guard_hw.py --port COM4
"""

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin import server as srv


_MP_VIDS = {0x2E8A, 0x16C0, 0x1A86, 0x10C4, 0x0403, 0x303A, 0x16D0}


def _autodetect_port() -> str:
    from serial.tools import list_ports
    candidates = [p.device for p in list_ports.comports() if p.vid in _MP_VIDS]
    if not candidates:
        raise RuntimeError(
            "No MicroPython port detected. Pass --port. "
            f"Saw: {[p.device for p in list_ports.comports()]}"
        )
    return candidates[0]


# Single-line statements only. MicroPython's normal REPL auto-indents
# continuation lines, so a multi-line ``while True:`` block sent line-by-
# line through ``send_line`` never terminates (the "blank" line we'd send
# to close the block gets the REPL's auto-indent prefix). Using
# semicolon-separated statements sidesteps this entirely.
INFINITE_PROGRAM = (
    "import time\n"
    "i = 0\n"
    "while True: print('tick', i); i = i + 1; time.sleep(0.1)\n"
)


async def _call(name: str, arguments: dict):
    return await srv.call_tool(name, arguments)


def _text(result) -> str:
    return result[0].text


async def run(port: str) -> int:
    print(f"=== streaming-guard HW test: {port} ===")
    failed = False

    try:
        # 1. Connect.
        out = _text(await _call("connect", {"port": port}))
        print(f"[STEP] connect -> {out!r}")
        assert "Connected" in out or "connected" in out.lower(), (
            f"connect didn't succeed: {out!r}"
        )

        # 2. start_program with an infinite loop.
        out = _text(await _call("start_program", {"code": INFINITE_PROGRAM}))
        print(f"[STEP] start_program -> {out!r}")
        # Give the REPL time to ingest the paste and start the loop.
        time.sleep(2.0)

        # 3. Drain REPL echo, then read again to find tick lines. The
        #    initial buffer contains '>>>' / '...' prompts the REPL emits
        #    while parsing the multi-line code; we discard those, then
        #    confirm the program is actually emitting 'tick' lines.
        _drain = _text(await _call("read_output", {"max_lines": 200, "wait": 0.5}))
        print(f"[STEP] drained-echo ({len(_drain)} chars)")
        # Wait for at least one tick after the echo settles.
        time.sleep(0.5)
        out = _text(await _call("read_output", {"max_lines": 20, "wait": 1.5}))
        print(f"[STEP] read_output -> {out[:200]!r}")
        assert "tick" in out, (
            f"Reader thread didn't capture program output: {out!r}\n"
            f"prior drain: {_drain[:200]!r}"
        )

        # 4. Try a non-safe tool — guard MUST reject without dispatching.
        out = _text(await _call("list_files", {"path": "/"}))
        print(f"[STEP] list_files (guarded) -> {out!r}")
        assert "streaming program is running" in out, (
            f"Guard didn't fire on list_files: {out!r}"
        )
        assert "would race the background reader thread" in out, out
        assert "list_files" in out, f"Error didn't name the rejected tool: {out!r}"
        # Crucially the device should NOT have produced fs_listdir output —
        # if it had, the next tick line would be garbled. We re-read to
        # confirm the stream is still clean.
        time.sleep(0.5)
        out2 = _text(await _call("read_output", {"max_lines": 20, "wait": 1.5}))
        print(f"[STEP] post-guard read_output -> {out2[:200]!r}")
        assert "tick" in out2, (
            f"Stream corrupted after guard test (no tick after reject): {out2!r}"
        )

        # 5. stop_program — must terminate cleanly (reader thread joins,
        #    runner.is_running() flips false).
        out = _text(await _call("stop_program", {}))
        print(f"[STEP] stop_program -> {out!r}")
        # Give the reader thread a moment to fully drain.
        time.sleep(0.3)

        # 6. After stop, list_files dispatches normally. The guard must
        #    release. Pass an empty path -> defaults to root.
        out = _text(await _call("list_files", {"path": "/"}))
        print(f"[STEP] post-stop list_files -> {out[:200]!r}")
        assert "streaming program is running" not in out, (
            f"Guard still firing after stop_program: {out!r}"
        )
        # And the response should look like a real listing (JSON array
        # of file entries). We only need the absence of the streaming
        # error to prove guard release; sanity-check parse anyway.
        try:
            parsed = json.loads(out)
            assert isinstance(parsed, list), f"Expected list of entries; got: {parsed!r}"
        except json.JSONDecodeError:
            # If parsing fails the listing might be plain text — that's
            # fine, the guard-release check above already passed.
            pass

        print("[PASS] streaming guard rejects raw-REPL tools and releases on stop")

    except AssertionError as e:
        failed = True
        print(f"[FAIL] {e}")
    except Exception as e:
        failed = True
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        # Best-effort cleanup: stop any leftover runner, then disconnect.
        try:
            await _call("stop_program", {})
        except Exception:
            pass
        try:
            await _call("disconnect", {})
        except Exception:
            pass

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port")
    args = parser.parse_args()
    port = args.port or _autodetect_port()
    return asyncio.run(run(port))


if __name__ == "__main__":
    sys.exit(main())
