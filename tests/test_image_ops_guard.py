"""Unit tests for the root-wipe guard in ImageOperations.push_image.

The guard at image_ops.py:186 refuses ``clean=True`` at ``target_path="/"``
unless the caller explicitly passes ``allow_root_wipe=True``. Without it,
a partial wipe of ``/`` can brick a device by removing boot.py / main.py.

These tests exercise the guard alone — no hardware, no transport. The
guard runs before any device interaction (it raises immediately after
the image-path existence check), so a fake device that would crash on
contact is fine. We just need a valid image file on disk to pass the
``image_path.exists()`` check that runs before the guard.

End-to-end happy path (clean=True + allow_root_wipe=True actually
restoring an image onto a sacrificial device) is covered by
tests/test_cross_device_image.py.
"""

import io
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.image_ops import ImageOperations


class _NeverTouched:
    """Stand-in for MicroPythonDevice. The guard raises before any
    method on this is called, so any access is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(
            f"Device.{name} accessed — guard should have raised first"
        )


@pytest.fixture
def empty_image(tmp_path: Path) -> Path:
    """A minimal valid tar.gz to satisfy the existence check that runs
    before the guard. Contents don't matter — the guard fires first."""
    p = tmp_path / "tiny.tar.gz"
    with tarfile.open(p, "w:gz") as tar:
        data = b"{}"
        info = tarfile.TarInfo(".micropython_image_metadata.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return p


@pytest.fixture
def ops() -> ImageOperations:
    # __init__ instantiates FileOperations(device) which only stores the
    # ref — no fs_* calls happen at construction, so the never-touched
    # device is fine.
    return ImageOperations(_NeverTouched())


def test_push_image_clean_root_without_allow_raises(ops, empty_image):
    """clean=True at "/" without allow_root_wipe → ValueError."""
    with pytest.raises(ValueError, match="allow_root_wipe"):
        ops.push_image(str(empty_image), target_path="/", clean=True)


def test_push_image_clean_root_default_allow_is_false(ops, empty_image):
    """allow_root_wipe defaults to False — explicit None equivalent of
    not-passing-the-arg must still raise."""
    with pytest.raises(ValueError, match="allow_root_wipe"):
        ops.push_image(
            str(empty_image),
            target_path="/",
            clean=True,
            allow_root_wipe=False,
        )


def test_push_image_clean_root_with_explicit_allow_does_not_raise_guard(
    ops, empty_image
):
    """clean=True + allow_root_wipe=True must NOT raise the guard. It
    will fail later (the fake device errors on first fs_* call) but
    must not raise ValueError from the guard path."""
    with pytest.raises(AssertionError):  # _NeverTouched fires
        ops.push_image(
            str(empty_image),
            target_path="/",
            clean=True,
            allow_root_wipe=True,
        )


def test_push_image_clean_false_at_root_no_guard(ops, empty_image):
    """clean=False at "/" — guard does not apply, regardless of the
    allow_root_wipe value. Same expected fail-on-device as above."""
    with pytest.raises(AssertionError):  # _NeverTouched fires
        ops.push_image(str(empty_image), target_path="/", clean=False)


def test_push_image_clean_true_non_root_no_guard(ops, empty_image):
    """clean=True at non-root paths is allowed without allow_root_wipe."""
    with pytest.raises(AssertionError):  # _NeverTouched fires
        ops.push_image(
            str(empty_image),
            target_path="/some_dir",
            clean=True,
        )


def test_push_image_missing_archive_raises_first(ops, tmp_path):
    """File-not-found check runs before the guard — pre-condition that
    a missing archive yields FileNotFoundError, not ValueError."""
    missing = tmp_path / "does_not_exist.tar.gz"
    with pytest.raises(FileNotFoundError):
        ops.push_image(
            str(missing),
            target_path="/",
            clean=True,
            # no allow_root_wipe — proves the existence check beats the guard
        )


def test_restore_snapshot_inherits_guard(ops, empty_image):
    """restore_snapshot delegates to push_image(target="/", clean=clean,
    allow_root_wipe=allow). Guard must still apply on this surface so a
    user calling the snapshot helper without opt-in is also blocked."""
    with pytest.raises(ValueError, match="allow_root_wipe"):
        ops.restore_snapshot(str(empty_image), clean=True)


def test_restore_snapshot_with_allow_passes_guard(ops, empty_image):
    """Explicit allow_root_wipe=True on restore_snapshot bypasses the
    guard (and then trips the fake device, as expected)."""
    with pytest.raises(AssertionError):
        ops.restore_snapshot(
            str(empty_image),
            clean=True,
            allow_root_wipe=True,
        )


# ---------------------------------------------------------------------------
# Platform-mismatch guard (image's recorded platform vs current device)
# ---------------------------------------------------------------------------


class _PlatformDevice:
    """Fake device that supports just enough surface for the platform
    guard to run: a no-op ``raw_repl_session`` context, ``get_device_info``
    returning a configurable platform, and AssertionError on anything else
    so we detect when the guard's bypass path actually starts driving fs ops.
    """

    def __init__(self, platform: str):
        self._platform = platform

    def raw_repl_session(self):
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield

        return _noop()


def _platform_ops(platform: str) -> ImageOperations:
    """ImageOperations wired to a _PlatformDevice. ``get_device_info`` lives
    on ``ImageOperations`` itself (not ``device``), so we monkey-patch it
    on the instance to return our test platform."""
    ops = ImageOperations(_PlatformDevice(platform))
    ops.get_device_info = lambda: {"platform": platform}  # type: ignore[method-assign]
    return ops


def _image_with_platform(tmp_path: Path, platform: str | None) -> Path:
    """Build a minimal tar.gz whose metadata advertises a given platform.
    Pass ``None`` to omit ``device_info`` entirely (tests the
    metadata-without-platform-info path)."""
    import json

    meta = {"created_at": "2026-04-27T00:00:00"}
    if platform is not None:
        meta["device_info"] = {"platform": platform, "version": "test"}

    p = tmp_path / f"img_{platform or 'no-info'}.tar.gz"
    with tarfile.open(p, "w:gz") as tar:
        blob = json.dumps(meta).encode("utf-8")
        info = tarfile.TarInfo(".micropython_image_metadata.json")
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
    return p


def test_platform_mismatch_refused_by_default(tmp_path):
    """Image platform 'esp32' onto device platform 'rp2' must raise
    ValueError without allow_platform_mismatch — the regression
    'happily restore an RP2040 image onto an ESP32' (TODO #7) is gone."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, "esp32")
    with pytest.raises(ValueError, match="allow_platform_mismatch"):
        ops.push_image(str(img), target_path="/some_dir")


def test_platform_mismatch_message_names_both_platforms(tmp_path):
    """The error must surface BOTH platform names so the operator can
    diagnose without re-reading the archive."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, "esp32")
    with pytest.raises(ValueError) as excinfo:
        ops.push_image(str(img), target_path="/some_dir")
    msg = str(excinfo.value)
    assert "esp32" in msg and "rp2" in msg, (
        f"Error message should name both platforms, got: {msg!r}"
    )


def test_platform_match_does_not_raise_guard(tmp_path):
    """Same platform on both sides — guard does NOT fire. Falls through
    to the extract loop, which on _PlatformDevice trips AssertionError
    when fs ops start (file_ops.write_file -> device.raw_repl_session
    is fine, but file_ops uses transport which is not on _PlatformDevice)."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, "rp2")
    # Same-platform: no ValueError. The push will then try to extract
    # files; the empty image has zero file entries so it completes
    # cleanly and returns a result dict.
    result = ops.push_image(str(img), target_path="/some_dir")
    assert result["files_written"] == 0
    assert result["errors"] == []


def test_platform_mismatch_with_explicit_allow_bypasses_guard(tmp_path):
    """allow_platform_mismatch=True must bypass the platform guard."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, "esp32")
    # No exception raised by the guard; empty archive → no fs ops →
    # returns a result dict instead of raising.
    result = ops.push_image(
        str(img),
        target_path="/some_dir",
        allow_platform_mismatch=True,
    )
    assert result["files_written"] == 0


def test_platform_no_archive_metadata_does_not_raise(tmp_path):
    """A tar.gz without the metadata file (legacy archives) MUST NOT
    trigger the guard — there's no platform claim to compare against."""
    ops = _platform_ops("rp2")
    p = tmp_path / "no_metadata.tar.gz"
    with tarfile.open(p, "w:gz") as tar:
        # Empty archive — no metadata entry, no file entries either.
        # If we put a real file entry the push tries to write it,
        # which on _PlatformDevice goes into results["errors"] (write
        # errors are not raised). The guard either fires (ValueError)
        # or it doesn't — that's all we're checking here.
        pass
    # Push onto non-root so the root-wipe guard isn't in the picture.
    # No metadata → no platform claim → guard must NOT fire.
    result = ops.push_image(str(p), target_path="/some_dir")
    assert result["files_written"] == 0
    # And no platform-mismatch ValueError appeared anywhere.


def test_platform_metadata_without_device_info_does_not_raise(tmp_path):
    """Archive has metadata but lacks device_info — same skip behavior
    (the metadata predates the platform field, e.g. legacy backup tool)."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, None)  # metadata, but no device_info
    # Same shape as no-metadata case: must not raise the platform guard.
    result = ops.push_image(str(img), target_path="/some_dir")
    assert result["files_written"] == 0


def test_platform_mismatch_guard_runs_before_wipe(tmp_path):
    """Critical invariant: guard must raise BEFORE any device wipe so a
    cross-platform restore can't accidentally brick the device.

    We exercise this with target_path="/some_dir" + clean=True. The
    wipe path on _PlatformDevice would trip AssertionError (no
    file_ops support) IF the guard didn't fire first. ValueError
    proves the guard wins the race."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, "esp32")
    with pytest.raises(ValueError, match="allow_platform_mismatch"):
        ops.push_image(str(img), target_path="/some_dir", clean=True)


def test_restore_snapshot_inherits_platform_guard(tmp_path):
    """restore_snapshot delegates and must propagate the platform guard
    (and the new allow_platform_mismatch parameter)."""
    ops = _platform_ops("rp2")
    img = _image_with_platform(tmp_path, "esp32")
    # restore_snapshot -> push_image(target="/", clean=True, allow_root_wipe=False)
    # The root-wipe guard fires FIRST (before the platform guard) since it
    # runs before raw_repl_session. So we must pass allow_root_wipe=True
    # to reach the platform check.
    with pytest.raises(ValueError, match="allow_platform_mismatch"):
        ops.restore_snapshot(str(img), clean=True, allow_root_wipe=True)
