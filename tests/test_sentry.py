"""Tests for abi_django_utils.sentry module."""

from unittest.mock import patch

from abi_django_utils import sentry


class TestInit:
    """Tests for sentry.init()."""

    def test_skips_when_no_dsn(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        assert sentry.init(debug=False) is False

    def test_skips_when_empty_dsn(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "")
        assert sentry.init(debug=False) is False

    def test_skips_when_debug_true(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        assert sentry.init(debug=True) is False

    @patch("sentry_sdk.init")
    def test_initializes_in_production(self, mock_sentry_init, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        result = sentry.init(debug=False)
        assert result is True
        mock_sentry_init.assert_called_once_with(
            dsn="https://key@sentry.io/123",
            traces_sample_rate=0.1,
            send_default_pii=False,
        )

    @patch("sentry_sdk.init")
    def test_custom_traces_rate(self, mock_sentry_init, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        sentry.init(debug=False, traces_sample_rate=0.5)
        mock_sentry_init.assert_called_once_with(
            dsn="https://key@sentry.io/123",
            traces_sample_rate=0.5,
            send_default_pii=False,
        )

    @patch("sentry_sdk.init")
    def test_zero_traces_rate(self, mock_sentry_init, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        sentry.init(debug=False, traces_sample_rate=0.0)
        mock_sentry_init.assert_called_once_with(
            dsn="https://key@sentry.io/123",
            traces_sample_rate=0.0,
            send_default_pii=False,
        )
