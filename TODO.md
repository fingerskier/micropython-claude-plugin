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

- [x] **6. Concurrent two-device sessions don't cross-talk.** ✅
  Implemented `tests/test_concurrent_devices.py` (COM4 + COM11). Four
  isolation invariants: (a) `_transport`, `transport.serial`, and `_lock`
  must be distinct per-instance; (b) opening `raw_repl_session` on A
  does NOT flip B's `transport.in_raw_repl`; (c) parallel `list_files("/")`
  on two threads finishes for both inside 20 s with no deadlock; (d)
  marker file written on A is invisible on B's listing AND reads back
  correctly on A — the strongest cross-talk smoke test, since a port
  mix-up or shared transport would either leak the marker or corrupt
  the read. Verified RED: temporarily aliased `self._lock` to a class
  attribute (`MicroPythonDevice._shared_red_lock`) → "Distinct per-device
  state" caught it. Reverted; 4/4 PASS, unit suite still 48/48.

- [x] **7. Push platform mismatch behavior is defined and tested.** ✅
  Decision: **refuse-by-default** with explicit `allow_platform_mismatch=True`
  bypass — mirrors the `allow_root_wipe` pattern. `image_ops.push_image` now
  pre-reads the archive's `.micropython_image_metadata.json`, gets the
  current device platform via `get_device_info()`, and raises `ValueError`
  when both are known and differ. Archives without metadata or without a
  `device_info.platform` field DO NOT trigger the guard (compatible with
  legacy backups). Guard runs BEFORE any device wipe so a cross-platform
  restore can't accidentally brick the device. `restore_snapshot`
  propagates the new arg. Added `tests/test_image_ops_guard.py` platform
  cases (8 new tests covering refuse default, error-message content,
  same-platform pass-through, opt-in bypass, no-metadata silence,
  metadata-without-device_info silence, guard-before-wipe ordering, and
  `restore_snapshot` inheritance). RED→GREEN: gated `if False and ...`
  → 4/16 fail; reverted → 16/16 pass; full unit suite 56/56;
  `test_hardware_eval.py` 21/21 (same-platform restore unaffected).

- [x] **8. `sync_file(NEWEST)` when device mtime is 0 / None.** ✅
  Fallback was real (`file_ops.py:292`: `remote_info.mtime or 0` → local
  always wins) but undocumented and untested. Added 3 unit tests in
  `tests/test_fileops_adapter.py::TestSyncFileNewestMtimeFallback`:
  (a) end-to-end via `fs_stat` returning `st_mtime=0` → upload; (b)
  direct `monkeypatch` stub of `get_file_info` returning `mtime=None`
  → upload; (c) edge case where BOTH local mtime (via `os.utime(p,
  (0,0))`) and remote mtime resolve to 0 → "in sync", no transfer
  (proves the fallback is symmetric, not absolute local-wins). Also
  added a docstring to `sync_file()` documenting the contract and
  pointing to the test class. Verified RED via mutation
  (`or 0` → `or 9999999999999`) → 3/3 fail; reverted → 3/3 pass; full
  unit suite 59/59. Hardware sanity script
  `tests/test_sync_file_newest_hw.py` confirms the behavior end-to-end
  on COM4 (Pimoroni Tiny 2040 reports a non-zero pseudo-RTC mtime
  ≈ 2021 epoch, so host's current Unix mtime always wins; local
  bytes uploaded and read back GREEN). Hardware eval regression
  21/21 PASS.

- [x] **9. Streaming guard rejects raw-REPL tools while a program runs.** ✅
  Two new test files. `tests/test_streaming_guard.py` (12 unit tests, no
  hardware): patches `server._runner` with a fake whose `is_running()`
  is the only honest attribute, then drives `srv.call_tool(...)` through
  asyncio. Covers (a) parametrized reject path for 7 raw-REPL-driving
  tools (`list_files`, `read_file`, `write_file`, `delete_file`,
  `execute_code`, `pull_image`, `push_image`) — each returns the
  streaming-active error naming the rejected tool; (b) safe-tool
  exemption (`list_devices` dispatches even when running); (c) no-runner
  short-circuit; (d) finished-runner short-circuit; (e) safe-set
  invariants — must contain `stop_program`/`interrupt`/`read_output`/
  `send_input` (recovery path) and must NOT contain raw-REPL drivers.
  RED: gated `if False and (...)` → 7/12 fail; reverted → 12/12 pass;
  full unit suite 71/71. Hardware test
  `tests/test_streaming_guard_hw.py` runs the full lifecycle on COM4:
  connects, `start_program` with `while True: print('tick',i); i+=1;
  time.sleep(0.1)` (single-line — multi-line blocks don't terminate
  through line-by-line REPL feeding because of MicroPython's auto-
  indent on continuation), drains REPL echo, confirms `tick` output,
  attempts `list_files` (rejected with the guard message), confirms
  the stream is still emitting `tick` lines uncorrupted, calls
  `stop_program`, then re-issues `list_files` and asserts a real JSON
  listing comes back (proves the guard releases). PASS on COM4.

- [x] **10. Disconnect / reconnect / serial-flake recovery.** ✅
  Implemented `tests/test_recovery.py` (10 unit tests, no hardware) using
  a `FakeTransport` that monkey-patches `serial_connection.SerialTransport`
  and serves a programmable script of effects (raise SerialException once,
  raise unconditionally, etc.). Coverage:
  * **Disconnect contracts** (5 tests): clean exit while in raw-REPL,
    no `exit_raw_repl` call when never entered, idempotent on
    never-connected, idempotent on second call, and crucially —
    swallows a raising `exit_raw_repl` so a leaked transport can't
    wedge the lock forever.
  * **Retry / recovery** (3 tests): one SerialException → reopen →
    second attempt succeeds (asserts new transport built); every
    attempt raises → SerialException propagates after 2 total
    attempts (initial + RECONNECT_RETRIES); TransportError converts
    to RuntimeError without retry (protocol error, not transport drop).
  * **Lock release** (2 tests): execute_raw raising leaves the lock
    available (verified with a thread + 5s join timeout); disconnect
    where transport.close() raises still releases the lock.

  **The retry test caught a real bug**: `execute_raw` captured
  `transport = self._ensure_transport()` once before the retry loop,
  so retries kept calling the OLD (closed) transport instead of the
  new one built by `_reopen()`. Fixed by moving the fetch inside the
  loop. RED confirmed by reverting just the fix → exhaustion test
  fails; re-applying the fix → all 10 pass.

  Hardware USB-yank scenario (yank cable during a large write,
  reconnect, retry) is necessarily out-of-scope for an unattended
  unit test — captured as Reqall #2141 (Pico W on COM11 sustained
  CDC writes ≥64 KiB).

  Verified RED → GREEN on the regression bug; full unit suite
  81/81; hardware eval 21/21 on COM4.

## Out of scope (already covered)
- Path sanitization (`#899`, 10 unit tests, all pass).
- `mpremote` SerialTransport adapter contract (`test_fileops_adapter.py`,
  29 tests).
- Single-device file ops up to 2.5 KiB (`test_hardware_eval.py` F01–F13).
