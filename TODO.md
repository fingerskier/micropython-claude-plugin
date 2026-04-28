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

- [x] **3. Strict assertions in `test_i07_compare_after_restore`.** ✅
  `compare_with_image` already compares by size + sha256 only (NOT mtime),
  so re-write timestamp drift cannot cause a false failure — documented
  this in the test docstring. Hardened assertions to require `different`,
  `only_on_device`, and `only_in_image` all `== []`, plus
  `len(matching) > 0` to catch a degenerate empty-archive case. Verified
  RED→GREEN on COM4: deliberate `assert different == ["__RED_PROOF__"]`
  caught (RED), reverted to `[]`, full hardware eval 21/21 PASS, unit
  tests still 40/40. Eval suite is now a real regression gate for image
  roundtrip.

- [x] **4. `push_image(clean=True, allow_root_wipe=True)` end-to-end.** ✅
  End-to-end happy path is already covered by `test_cross_device_image.py`
  (TODO #1): A → image → wipe B → restore → exact-match assertion. The
  remaining gap was the **guard** path. Added `tests/test_image_ops_guard.py`
  (8 tests, no hardware): asserts `ValueError` raised when `clean=True,
  target_path='/'` without `allow_root_wipe` (and via `restore_snapshot`),
  asserts the guard does NOT fire for `clean=False`, non-root targets, or
  explicit `allow_root_wipe=True`, and asserts `FileNotFoundError` beats the
  guard so users get the more specific error first. Verified RED→GREEN by
  flipping the source guard's predicate to `False` (3 tests fail), then
  reverting (8/8 pass; full unit suite now 48/48).

- [x] **5. `sync_directory` with `delete_orphans`, both directions.** ✅
  Implemented `tests/test_sync_directory_orphans.py` (hardware, sandbox at
  `/sync_orphan_test`). Three subtests: (a) UPLOAD → seed three files,
  delete one locally, re-sync with `delete_orphans=False` and assert remote
  still has all three (proves opt-in default), then re-sync with
  `delete_orphans=True` and assert orphan gone; (b) DOWNLOAD mirror with the
  one-file-deleted-on-device case; (c) NEWEST + `delete_orphans=True` is a
  no-op — asserts the documented "delete_orphans ignored" warning in the
  results list and asserts no files vanished from either side. Sandbox
  wiped on entry/exit so the device's normal filesystem is untouched.
  Verified RED (mutated `if delete_orphans:` to `if False and delete_orphans:`
  → 3/3 fail) → GREEN on COM4 (3/3 PASS, unit suite still 48/48).

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
