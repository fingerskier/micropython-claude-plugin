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
