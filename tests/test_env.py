"""Tests for abi_django_utils.env module."""

from abi_django_utils import env


class TestStr:
    """Tests for env.str()."""

    def test_reads_value(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        assert env.str("TEST_VAR") == "hello"

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.str("TEST_VAR", default="fallback") == "fallback"

    def test_missing_without_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.str("TEST_VAR") == ""

    def test_preserves_whitespace(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "  spaced  ")
        assert env.str("TEST_VAR") == "  spaced  "


class TestBool:
    """Tests for env.bool()."""

    def test_truthy_values(self, monkeypatch):
        for value in ["1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"]:
            monkeypatch.setenv("TEST_VAR", value)
            assert env.bool("TEST_VAR") is True, f"Failed for value: {value}"

    def test_falsy_values(self, monkeypatch):
        for value in ["0", "false", "False", "no", "No", "off", "Off", "", "random"]:
            monkeypatch.setenv("TEST_VAR", value)
            assert env.bool("TEST_VAR") is False, f"Failed for value: {value}"

    def test_whitespace_handling(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "  true  ")
        assert env.bool("TEST_VAR") is True

    def test_missing_with_default_true(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.bool("TEST_VAR", default=True) is True

    def test_missing_with_default_false(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.bool("TEST_VAR", default=False) is False

    def test_missing_without_explicit_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.bool("TEST_VAR") is False


class TestInt:
    """Tests for env.int()."""

    def test_reads_integer(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "42")
        assert env.int("TEST_VAR") == 42

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "  7  ")
        assert env.int("TEST_VAR") == 7

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.int("TEST_VAR", default=99) == 99

    def test_missing_without_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.int("TEST_VAR") == 0

    def test_negative_value(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "-5")
        assert env.int("TEST_VAR") == -5

    def test_invalid_raises_with_var_name(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "not_a_number")
        import pytest

        with pytest.raises(ValueError, match="TEST_VAR"):
            env.int("TEST_VAR")


class TestList:
    """Tests for env.list()."""

    def test_comma_separated_values(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "foo,bar,baz")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]

    def test_whitespace_stripping(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "  foo , bar , baz  ")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]

    def test_empty_items_dropped(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "foo,,bar,  ,baz")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]

    def test_empty_string(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "")
        assert env.list("TEST_VAR") == []

    def test_missing_with_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.list("TEST_VAR", default=["x", "y"]) == ["x", "y"]

    def test_missing_without_default(self, monkeypatch):
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.list("TEST_VAR") == []

    def test_single_value(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "solo")
        assert env.list("TEST_VAR") == ["solo"]
