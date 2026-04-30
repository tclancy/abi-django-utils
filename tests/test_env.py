"""Tests for abi_django_utils.env module."""

from abi_django_utils import env


class TestBool:
    """Tests for env.bool()."""

    def test_truthy_values(self, monkeypatch):
        """Test that truthy values are recognized."""
        truthy_values = ["1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"]
        for value in truthy_values:
            monkeypatch.setenv("TEST_VAR", value)
            assert env.bool("TEST_VAR") is True, f"Failed for value: {value}"

    def test_falsy_values(self, monkeypatch):
        """Test that falsy values return False."""
        falsy_values = ["0", "false", "False", "no", "No", "off", "Off", "", "random"]
        for value in falsy_values:
            monkeypatch.setenv("TEST_VAR", value)
            assert env.bool("TEST_VAR") is False, f"Failed for value: {value}"

    def test_whitespace_handling(self, monkeypatch):
        """Test that whitespace is stripped."""
        monkeypatch.setenv("TEST_VAR", "  true  ")
        assert env.bool("TEST_VAR") is True

        monkeypatch.setenv("TEST_VAR", "  1  ")
        assert env.bool("TEST_VAR") is True

    def test_missing_with_default_true(self, monkeypatch):
        """Test that missing env var returns default=True."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.bool("TEST_VAR", default=True) is True

    def test_missing_with_default_false(self, monkeypatch):
        """Test that missing env var returns default=False."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.bool("TEST_VAR", default=False) is False

    def test_missing_without_explicit_default(self, monkeypatch):
        """Test that missing env var returns False by default."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.bool("TEST_VAR") is False


class TestList:
    """Tests for env.list()."""

    def test_comma_separated_values(self, monkeypatch):
        """Test parsing comma-separated values."""
        monkeypatch.setenv("TEST_VAR", "foo,bar,baz")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]

    def test_whitespace_stripping(self, monkeypatch):
        """Test that whitespace is stripped from items."""
        monkeypatch.setenv("TEST_VAR", "  foo , bar , baz  ")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]

    def test_empty_items_dropped(self, monkeypatch):
        """Test that empty items are dropped."""
        monkeypatch.setenv("TEST_VAR", "foo,,bar,  ,baz")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]

    def test_empty_string(self, monkeypatch):
        """Test that empty env var returns empty list."""
        monkeypatch.setenv("TEST_VAR", "")
        assert env.list("TEST_VAR") == []

    def test_missing_with_default(self, monkeypatch):
        """Test that missing env var returns default."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.list("TEST_VAR", default=["x", "y"]) == ["x", "y"]

    def test_missing_without_default(self, monkeypatch):
        """Test that missing env var returns empty list by default."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env.list("TEST_VAR") == []

    def test_single_value(self, monkeypatch):
        """Test that a single value is wrapped in a list."""
        monkeypatch.setenv("TEST_VAR", "solo")
        assert env.list("TEST_VAR") == ["solo"]

    def test_complex_whitespace(self, monkeypatch):
        """Test handling of tabs and newlines (if present)."""
        monkeypatch.setenv("TEST_VAR", "foo,\tbar  ,  baz")
        assert env.list("TEST_VAR") == ["foo", "bar", "baz"]
