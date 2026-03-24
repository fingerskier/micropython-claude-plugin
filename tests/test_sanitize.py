"""Unit tests for path sanitization — no hardware required."""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from micropython_claude_plugin.file_ops import _sanitize_path


class TestSanitizePath:
    def test_normal_path(self):
        assert _sanitize_path("/main.py") == "/main.py"

    def test_nested_path(self):
        assert _sanitize_path("/lib/module/foo.py") == "/lib/module/foo.py"

    def test_root(self):
        assert _sanitize_path("/") == "/"

    def test_normalizes_double_slash(self):
        assert _sanitize_path("//lib//foo.py") == "/lib/foo.py"

    def test_rejects_double_quote(self):
        with pytest.raises(ValueError, match="forbidden"):
            _sanitize_path('/test"; import os; os.remove("/boot.py")')

    def test_rejects_single_quote(self):
        with pytest.raises(ValueError, match="forbidden"):
            _sanitize_path("/test'; print('hacked')")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError, match="forbidden"):
            _sanitize_path("/test\\file")

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError, match="forbidden"):
            _sanitize_path("/test; rm -rf /")

    def test_rejects_newline(self):
        with pytest.raises(ValueError, match="forbidden"):
            _sanitize_path("/test\nimport os")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="forbidden"):
            _sanitize_path("/test\x00file")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
