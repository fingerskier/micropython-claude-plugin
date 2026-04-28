# TODO

Top 10 validation & testing gaps. Two MicroPython devices (e.g. `COM3` + `COM4`)
are connected; tests requiring two devices use one as the golden source and one
as a sacrificial target.

_Generated 2026-04-27. Sources: Reqall (project #842), GitHub
`fingerskier/micropython-claude-plugin` (0 open issues), local
`tests/test_hardware_eval.py` + `tests/test_fileops_adapter.py`._

## Coverage today
- 21/21 single-device hardware eval pass on COM4 (RP2040, MicroPython 1.21.0).
- 40/40 unit tests pass (`test_sanitize.py`, `test_fileops_adapter.py`).
- No cross-device tests. No large/binary stress. No clean-wipe restore.
  `compare_image` after restore reports diffs but does **not** assert.
- `sync_directory`, `delete_orphans`, streaming guard, push platform-mismatch,
  reconnect/recovery — all untested.

## Top 10

- [x] **1. Cross-device image roundtrip (A → image → B, exact match).** ✅
  Implemented `tests/test_cross_device_image.py`. Pulls image from A, backs up
  B, restores A's image onto B with `clean=True, allow_root_wipe=True`,
  asserts `different/only_on_device/only_in_image` are all `[]` and that
  `matching` covers the full archive payload. Restores B from backup in a
  `finally` so the test is non-destructive. Verified RED (deliberate
  `assert False`) → GREEN on hardware (COM4 + COM11, RP2040): 7 files, 34464
  bytes, 0 diffs, 3941 ms. Auto-detects two MicroPython VIDs when ports are
  omitted.

- [x] **2. Large + binary file round-trip stress.** ✅
  Implemented `tests/test_binary_stress.py`. Writes `os.urandom(N)` payloads
  at 10 / 64 / 256 KiB, verifies via `write_file(verify=True)` (which exercises
  `fs_hashfile` against the host sha256), then re-reads through `fs_readfile`
  and asserts byte-exact equality with first-diff offset on mismatch. RED
  proof via 1-byte XOR corruption caught all three sizes. GREEN on COM4
  (Pimoroni Tiny 2040, MicroPython 1.21.0): 10 KiB write/read 593/747 ms,
  64 KiB 3.2/4.7 s, 256 KiB 12.5/17.9 s — ~20 KiB/s write, ~14 KiB/s read.
  COM11 (Pico W) passes 10 KiB but loses USB enumeration at 64 KiB+
  (`WriteFile failed PermissionError(13)` from the Win32 layer) — captured
  separately as a hardware-side finding, not a plugin defect.

- [ ] **3. Strict assertions in `test_i07_compare_after_restore`.**
  Today the test only reports diffs. Tighten to `assert different == []` after
  `push_image(clean=False)`; if mtime drift makes that flaky, restrict the
  assert to size + sha256 (already what `compare_with_image` uses) and document
  why mtime is excluded. Required to make the eval suite a real regression
  gate.

- [ ] **4. `push_image(clean=True, allow_root_wipe=True)` end-to-end.**
  Never exercised in hardware. Use device B as the wipe target: pull golden
  image from A, wipe B, restore, assert match against image. Verify the guard
  on `clean=True, target_path='/'` without `allow_root_wipe` still raises
  `ValueError` (unit test against `image_ops.py` is fine for the guard alone).

- [ ] **5. `sync_directory` with `delete_orphans`, both directions.**
  No coverage today. Build a small local tree, `sync_directory(direction=upload,
  delete_orphans=False)`, delete one local file, re-sync with
  `delete_orphans=True`, assert remote orphan was removed. Mirror test for
  `download`. Assert NEWEST + `delete_orphans=True` is a no-op (matches
  documented behavior in `file_ops.py:389`).

- [ ] **6. Concurrent two-device sessions don't cross-talk.**
  Construct two `MicroPythonDevice` instances (COM3, COM4) in one process,
  open `raw_repl_session` on both, run `list_files("/")` on each, and verify
  per-device `transport.serial` isolation (no shared global state, no port
  mix-up). Catches regressions in `serial_connection.py` if the lock or
  transport handle ever becomes module-scoped.

- [ ] **7. Push platform mismatch behavior is defined and tested.**
  Today `push_image` will happily restore an RP2040 image onto an ESP32. Decide:
  warn, refuse, or write a `metadata.device_info.platform` mismatch field. Add a
  test that pulls from A, fakes a different `platform` in the archive
  metadata, and asserts the chosen behavior. (If kept silent, document it in
  the tool description.)

- [ ] **8. `sync_file(NEWEST)` when device mtime is 0 / None.**
  `file_ops.py:292` falls back to `0`, so local always wins on littlefs targets
  without an RTC. Untested and surprising. Add a test that stubs
  `get_file_info` to return `mtime=None`, asserts the documented fallback, and
  add a hardware case on a device whose `os.stat` returns 0 mtime (most RP2040
  builds).

- [ ] **9. Streaming guard rejects raw-REPL tools while a program runs.**
  `server.py:419` defines `_STREAMING_SAFE_TOOLS`. No test exercises the
  reject path. Add a hardware test: `start_program` with an infinite loop on
  device A, then call `list_files` via the server dispatch and assert the
  returned `TextContent` says "would race the background reader thread", then
  `stop_program` cleanly exits. Prevents reader-thread / raw-REPL data races.

- [ ] **10. Disconnect / reconnect / serial-flake recovery.**
  No test confirms the plugin recovers from USB drop. Required scenarios:
  (a) call `disconnect` mid raw-REPL session; (b) yank USB on device B during a
  large `write_file`, reconnect, retry, assert success; (c) post-error
  `_device_lock` is released and subsequent tool calls don't deadlock. Some
  parts can be unit-tested with a fake transport that raises `TransportError`
  mid-call; the USB-yank case is hardware-only.

## Out of scope (already covered)
- Path sanitization (`#899`, 10 unit tests, all pass).
- `mpremote` SerialTransport adapter contract (`test_fileops_adapter.py`,
  29 tests).
- Single-device file ops up to 2.5 KiB (`test_hardware_eval.py` F01–F13).
