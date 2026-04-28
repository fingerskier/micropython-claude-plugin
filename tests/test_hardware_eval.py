"""
Hardware evaluation tests for MicroPython Claude Plugin.

Runs file operations and image operations against a real Pico W on COM3.
Prints PASS/FAIL for each test and cleans up after itself.

Usage:
    python tests/test_hardware_eval.py [--port COM3]
"""

import os
import sys
import time
import json
import tarfile
import argparse
import traceback
from pathlib import Path

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.serial_connection import MicroPythonDevice
from micropython_claude_plugin.file_ops import FileOperations
from micropython_claude_plugin.image_ops import ImageOperations


# ---------------------------------------------------------------------------
# Test runner infrastructure
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self, test_id: str, name: str):
        self.test_id = test_id
        self.name = name
        self.passed = False
        self.detail = ""
        self.elapsed_ms = 0

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.test_id}: {self.name} ({self.elapsed_ms}ms) {self.detail}"


results: list[TestResult] = []


def run_test(test_id: str, name: str, fn, *args, **kwargs) -> TestResult:
    """Run a single test function, catching exceptions."""
    result = TestResult(test_id, name)
    t0 = time.time()
    try:
        fn(result, *args, **kwargs)
    except Exception as e:
        result.passed = False
        result.detail = f"EXCEPTION: {e}\n{traceback.format_exc()}"
    result.elapsed_ms = int((time.time() - t0) * 1000)
    results.append(result)
    print(result)
    return result


# ---------------------------------------------------------------------------
# File operations tests
# ---------------------------------------------------------------------------

def test_f01_connect(r: TestResult, device: MicroPythonDevice):
    """Connect to COM3 and verify raw REPL."""
    device.connect()
    assert device.is_connected, "Device not connected after connect()"
    # Quick execute to prove raw REPL works
    out = device.execute("print('ping')")
    assert "ping" in out, f"Expected 'ping' in output, got: {out!r}"
    r.passed = True
    r.detail = "Connected and raw REPL verified"


def test_f02_list_files(r: TestResult, file_ops: FileOperations):
    """List root directory."""
    entries = file_ops.list_files("/")
    assert isinstance(entries, list), f"Expected list, got {type(entries)}"
    assert len(entries) > 0, "Root listing returned 0 entries"
    # Check structure
    e = entries[0]
    assert hasattr(e, "name"), "FileInfo missing 'name'"
    assert hasattr(e, "size"), "FileInfo missing 'size'"
    assert hasattr(e, "is_dir"), "FileInfo missing 'is_dir'"
    assert hasattr(e, "mtime"), "FileInfo missing 'mtime'"
    r.passed = True
    names = [x.name for x in entries]
    r.detail = f"{len(entries)} entries: {names}"


def test_f03_write_read_basic(r: TestResult, file_ops: FileOperations):
    """Write 'hello pico' and read it back."""
    content = b"hello pico"
    file_ops.write_file("/test_eval.txt", content)
    got = file_ops.read_file("/test_eval.txt")
    assert got == content, f"Content mismatch: wrote {content!r}, read {got!r}"
    r.passed = True
    r.detail = f"Wrote and read back {len(content)} bytes"


def test_f04_write_read_empty(r: TestResult, file_ops: FileOperations):
    """Write empty file, read back."""
    content = b""
    file_ops.write_file("/test_eval_empty.txt", content)
    got = file_ops.read_file("/test_eval_empty.txt")
    assert got == content, f"Expected empty bytes, got {got!r} (len={len(got)})"
    r.passed = True
    r.detail = "0-byte file round-trip OK"


def test_f05_write_read_large(r: TestResult, file_ops: FileOperations):
    """Write 2.5KB file, read back."""
    # Create deterministic content larger than CHUNK_SIZE (512)
    content = bytes(range(256)) * 10  # 2560 bytes
    file_ops.write_file("/test_eval_large.txt", content)
    got = file_ops.read_file("/test_eval_large.txt")
    assert got == content, (
        f"Content mismatch: wrote {len(content)} bytes, read {len(got)} bytes. "
        f"First diff at byte {next((i for i in range(min(len(got), len(content))) if got[i] != content[i]), 'N/A')}"
    )
    r.passed = True
    r.detail = f"2560 bytes round-trip OK"


def test_f06_write_read_unicode(r: TestResult, file_ops: FileOperations):
    """Write unicode content (multi-byte UTF-8)."""
    text = "caf\u00e9 \u2603 \u00e9"
    content = text.encode("utf-8")
    file_ops.write_file("/test_eval_unicode.txt", content)
    got = file_ops.read_file("/test_eval_unicode.txt")
    assert got == content, f"Unicode mismatch: wrote {content!r}, read {got!r}"
    r.passed = True
    r.detail = f"UTF-8 content ({len(content)} bytes) round-trip OK"


def test_f07_mkdir_single(r: TestResult, file_ops: FileOperations):
    """Create single directory."""
    file_ops.mkdir("/test_eval_dir", exist_ok=True)
    assert file_ops.file_exists("/test_eval_dir"), "Directory not found after mkdir"
    info = file_ops.get_file_info("/test_eval_dir")
    assert info is not None and info.is_dir, "Expected directory, got file or None"
    r.passed = True
    r.detail = "Single directory created"


def test_f08_mkdir_nested(r: TestResult, file_ops: FileOperations):
    """Create nested directories."""
    file_ops.mkdir("/test_eval_dir/a/b/c", exist_ok=True)
    assert file_ops.file_exists("/test_eval_dir/a/b/c"), "Nested dir not found"
    assert file_ops.file_exists("/test_eval_dir/a/b"), "Intermediate dir /a/b not found"
    assert file_ops.file_exists("/test_eval_dir/a"), "Intermediate dir /a not found"
    r.passed = True
    r.detail = "Nested dirs /a/b/c created with intermediates"


def test_f09_file_in_subdir(r: TestResult, file_ops: FileOperations):
    """Write file into nested dir, list, read back."""
    content = b"nested file content"
    file_ops.write_file("/test_eval_dir/a/b/c/nested.txt", content)

    # List the directory
    entries = file_ops.list_files("/test_eval_dir/a/b/c")
    names = [e.name for e in entries]
    assert "nested.txt" in names, f"nested.txt not in listing: {names}"

    # Read back
    got = file_ops.read_file("/test_eval_dir/a/b/c/nested.txt")
    assert got == content, f"Content mismatch in nested file"
    r.passed = True
    r.detail = f"Subdirectory file ops OK, listing: {names}"


def test_f10_metadata(r: TestResult, file_ops: FileOperations):
    """file_exists and get_file_info checks."""
    # Existing file
    assert file_ops.file_exists("/test_eval.txt"), "test_eval.txt should exist"

    info = file_ops.get_file_info("/test_eval.txt")
    assert info is not None, "get_file_info returned None for existing file"
    assert info.name == "test_eval.txt", f"Name mismatch: {info.name}"
    assert info.size == len(b"hello pico"), f"Size mismatch: {info.size}"
    assert info.is_dir is False, "Should not be a directory"

    # Non-existent file
    assert not file_ops.file_exists("/nonexistent_xyz.txt"), "Phantom file exists"
    info2 = file_ops.get_file_info("/nonexistent_xyz.txt")
    assert info2 is None, "get_file_info should return None for missing file"

    r.passed = True
    r.detail = f"Metadata OK: name={info.name}, size={info.size}, is_dir={info.is_dir}"


def test_f11_overwrite(r: TestResult, file_ops: FileOperations):
    """Overwrite existing file with new content."""
    original = b"hello pico"
    new_content = b"overwritten content that is longer"
    file_ops.write_file("/test_eval.txt", new_content)
    got = file_ops.read_file("/test_eval.txt")
    assert got == new_content, f"Overwrite failed: got {got!r}"
    assert got != original, "Content not changed"
    r.passed = True
    r.detail = f"Overwrite OK ({len(original)} -> {len(new_content)} bytes)"


def test_f12_delete_files(r: TestResult, file_ops: FileOperations):
    """Delete test files."""
    test_files = [
        "/test_eval.txt",
        "/test_eval_empty.txt",
        "/test_eval_large.txt",
        "/test_eval_unicode.txt",
    ]
    deleted = []
    for f in test_files:
        if file_ops.file_exists(f):
            file_ops.delete_file(f)
            assert not file_ops.file_exists(f), f"File {f} still exists after delete"
            deleted.append(f)
    r.passed = True
    r.detail = f"Deleted {len(deleted)} files: {deleted}"


def test_f13_rmdir_recursive(r: TestResult, file_ops: FileOperations):
    """Remove test directories recursively."""
    if file_ops.file_exists("/test_eval_dir"):
        file_ops.rmdir("/test_eval_dir", recursive=True)
        assert not file_ops.file_exists("/test_eval_dir"), "Dir still exists after rmdir"
    r.passed = True
    r.detail = "Recursive rmdir OK"


# ---------------------------------------------------------------------------
# Image operations tests
# ---------------------------------------------------------------------------

IMAGE_PATH = Path("./test_device_image.tar.gz")


def test_i01_device_info(r: TestResult, image_ops: ImageOperations):
    """get_device_info returns expected fields."""
    info = image_ops.get_device_info()
    assert isinstance(info, dict), f"Expected dict, got {type(info)}"

    expected_keys = ["platform", "version"]
    for key in expected_keys:
        assert key in info, f"Missing key: {key}"

    r.passed = True
    # Summarize what we got
    summary_keys = ["platform", "version", "mem_free", "mem_alloc", "freq", "unique_id"]
    summary = {k: info.get(k) for k in summary_keys if k in info}
    r.detail = f"Device info: {json.dumps(summary, default=str)}"


def test_i02_pull_image(r: TestResult, image_ops: ImageOperations):
    """Pull full device image to tar.gz."""
    if IMAGE_PATH.exists():
        IMAGE_PATH.unlink()

    metadata = image_ops.pull_image(str(IMAGE_PATH))
    assert IMAGE_PATH.exists(), "Image file not created"
    assert IMAGE_PATH.stat().st_size > 0, "Image file is empty"
    assert metadata.file_count > 0, f"No files in image (file_count={metadata.file_count})"
    assert metadata.total_size > 0, f"Zero total_size"

    r.passed = True
    r.detail = (
        f"Pulled {metadata.file_count} files, "
        f"{metadata.total_size} bytes, "
        f"archive={IMAGE_PATH.stat().st_size} bytes"
    )


def test_i03_inspect_archive(r: TestResult):
    """Inspect tar.gz contents and verify structure."""
    assert IMAGE_PATH.exists(), "Image file missing"

    metadata_found = False
    file_entries = []

    with tarfile.open(IMAGE_PATH, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == ".micropython_image_metadata.json":
                metadata_found = True
                f = tar.extractfile(member)
                meta = json.loads(f.read().decode("utf-8"))
                assert "device_info" in meta, "Metadata missing device_info"
                assert "created_at" in meta, "Metadata missing created_at"
            else:
                file_entries.append(member.name)

    assert metadata_found, "Metadata JSON not found in archive"
    assert len(file_entries) > 0, "No file entries in archive"

    r.passed = True
    r.detail = f"Archive OK: metadata + {len(file_entries)} files: {file_entries[:5]}..."


def test_i04_write_marker(r: TestResult, file_ops: FileOperations):
    """Write marker file to device for compare test."""
    content = b"eval_marker_content_12345"
    file_ops.write_file("/eval_marker.txt", content)
    got = file_ops.read_file("/eval_marker.txt")
    assert got == content, f"Marker write/read failed"
    r.passed = True
    r.detail = "Marker file written"


def test_i05_compare_with_marker(r: TestResult, image_ops: ImageOperations):
    """Compare device with image - marker should be only_on_device."""
    diff = image_ops.compare_with_image(str(IMAGE_PATH))
    assert isinstance(diff, dict), f"Expected dict, got {type(diff)}"

    # The marker file should appear in only_on_device
    only_device = diff.get("only_on_device", [])
    marker_found = any("eval_marker" in p for p in only_device)
    assert marker_found, (
        f"eval_marker.txt not in only_on_device. "
        f"only_on_device={only_device}, matching={diff.get('matching', [])}"
    )

    r.passed = True
    r.detail = (
        f"Compare: {len(diff.get('matching', []))} matching, "
        f"{len(diff.get('different', []))} different, "
        f"{len(only_device)} only_on_device, "
        f"{len(diff.get('only_in_image', []))} only_in_image"
    )


def test_i06_delete_marker_push_image(r: TestResult, file_ops: FileOperations, image_ops: ImageOperations):
    """Delete marker and push image back to restore state."""
    # Delete marker
    file_ops.delete_file("/eval_marker.txt")
    assert not file_ops.file_exists("/eval_marker.txt"), "Marker still exists"

    # Push image back (non-clean since target is /)
    result = image_ops.push_image(str(IMAGE_PATH), target_path="/", clean=False)
    assert result["files_written"] > 0, f"No files written during push"
    assert len(result.get("errors", [])) == 0, f"Push errors: {result['errors']}"

    r.passed = True
    r.detail = (
        f"Pushed {result['files_written']} files, "
        f"{result['bytes_written']} bytes, "
        f"{len(result.get('errors', []))} errors"
    )


def test_i07_compare_after_restore(r: TestResult, image_ops: ImageOperations):
    """Compare after restore - device must match image exactly.

    image_ops.compare_with_image() compares by size + sha256 only (NOT
    mtime), so re-write timestamp drift cannot cause a false failure.
    After test_i06's push (which restored the image we pulled in i01),
    every file in the image must be on the device with matching content,
    and the device must hold no files outside the image.
    """
    diff = image_ops.compare_with_image(str(IMAGE_PATH))

    only_device = diff.get("only_on_device", [])
    only_image = diff.get("only_in_image", [])
    different = diff.get("different", [])
    matching = diff.get("matching", [])

    r.detail = (
        f"After restore: {len(matching)} matching, "
        f"{len(different)} different, "
        f"{len(only_device)} only_on_device, "
        f"{len(only_image)} only_in_image"
    )
    if only_image:
        r.detail += f" | only_in_image: {only_image}"
    if only_device:
        r.detail += f" | only_on_device: {only_device}"
    if different:
        r.detail += f" | different: {different}"

    assert different == [], (
        f"Files with content/size diffs after restore: {different}"
    )
    assert only_image == [], (
        f"Image has files not on device after restore: {only_image}"
    )
    assert only_device == [], (
        f"Device has files not in image after restore: {only_device}"
    )
    assert len(matching) > 0, "compare_with_image returned zero matches"

    r.passed = True


def test_i08_cleanup_archive(r: TestResult):
    """Remove local test archive."""
    if IMAGE_PATH.exists():
        IMAGE_PATH.unlink()
    assert not IMAGE_PATH.exists(), "Archive not deleted"
    r.passed = True
    r.detail = "Local archive cleaned up"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Hardware eval tests for MicroPython plugin")
    parser.add_argument("--port", default="COM3", help="Serial port (default: COM3)")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()

    print(f"{'='*70}")
    print(f"MicroPython Plugin Hardware Evaluation")
    print(f"Port: {args.port}  Baudrate: {args.baudrate}")
    print(f"{'='*70}\n")

    device = MicroPythonDevice(args.port, args.baudrate)
    file_ops = FileOperations(device)
    image_ops = ImageOperations(device)

    t_start = time.time()

    # --- File operation tests ---
    print("--- File Operations ---")
    run_test("F01", "Connect to device", test_f01_connect, device)

    if not device.is_connected:
        print("\nFATAL: Cannot connect. Aborting remaining tests.")
        print_summary(t_start)
        return 1

    run_test("F02", "List files /", test_f02_list_files, file_ops)
    run_test("F03", "Write/read basic text", test_f03_write_read_basic, file_ops)
    run_test("F04", "Write/read empty file", test_f04_write_read_empty, file_ops)
    run_test("F05", "Write/read 2.5KB file", test_f05_write_read_large, file_ops)
    run_test("F06", "Write/read unicode", test_f06_write_read_unicode, file_ops)
    run_test("F07", "mkdir single", test_f07_mkdir_single, file_ops)
    run_test("F08", "mkdir nested /a/b/c", test_f08_mkdir_nested, file_ops)
    run_test("F09", "File in subdirectory", test_f09_file_in_subdir, file_ops)
    run_test("F10", "Metadata queries", test_f10_metadata, file_ops)
    run_test("F11", "Overwrite existing file", test_f11_overwrite, file_ops)
    run_test("F12", "Delete test files", test_f12_delete_files, file_ops)
    run_test("F13", "rmdir recursive", test_f13_rmdir_recursive, file_ops)

    # --- Image operation tests ---
    print("\n--- Image Operations ---")
    run_test("I01", "Get device info", test_i01_device_info, image_ops)
    run_test("I02", "Pull full image", test_i02_pull_image, image_ops)
    run_test("I03", "Inspect archive", test_i03_inspect_archive)
    run_test("I04", "Write marker file", test_i04_write_marker, file_ops)
    run_test("I05", "Compare with marker", test_i05_compare_with_marker, image_ops)
    run_test("I06", "Delete marker + push image", test_i06_delete_marker_push_image, file_ops, image_ops)
    run_test("I07", "Compare after restore", test_i07_compare_after_restore, image_ops)
    run_test("I08", "Cleanup archive", test_i08_cleanup_archive)

    # --- Cleanup & disconnect ---
    print("\n--- Cleanup ---")
    # Extra safety: remove any leftover test artifacts on device
    for path in ["/test_eval.txt", "/test_eval_empty.txt", "/test_eval_large.txt",
                 "/test_eval_unicode.txt", "/eval_marker.txt"]:
        try:
            if file_ops.file_exists(path):
                file_ops.delete_file(path)
                print(f"  Cleaned up leftover: {path}")
        except Exception:
            pass
    try:
        if file_ops.file_exists("/test_eval_dir"):
            file_ops.rmdir("/test_eval_dir", recursive=True)
            print("  Cleaned up leftover: /test_eval_dir")
    except Exception:
        pass

    device.disconnect()
    print("  Device disconnected.")

    return print_summary(t_start)


def print_summary(t_start: float) -> int:
    total_ms = int((time.time() - t_start) * 1000)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)

    print(f"\n{'='*70}")
    print(f"SUMMARY: {passed} passed, {failed} failed, {len(results)} total ({total_ms}ms)")
    print(f"{'='*70}")

    if failed:
        print("\nFailed tests:")
        for r in results:
            if not r.passed:
                print(f"  {r}")
        return 1
    else:
        print("\nAll tests passed!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
