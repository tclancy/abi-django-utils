"""Tests for abi_django_utils.tls_proxy module."""

from abi_django_utils import tls_proxy


class TestCsrfTrustedOrigins:
    """Tests for csrf_trusted_origins()."""

    def test_single_public_host(self):
        """Test promotion of a single public host."""
        result = tls_proxy.csrf_trusted_origins(["example.com"])
        assert result == ["https://example.com"]

    def test_multiple_public_hosts(self):
        """Test promotion of multiple public hosts."""
        result = tls_proxy.csrf_trusted_origins(["example.com", "api.example.com", "app.example.org"])
        assert result == [
            "https://example.com",
            "https://api.example.com",
            "https://app.example.org",
        ]

    def test_localhost_filtered(self):
        """Test that localhost is filtered out."""
        result = tls_proxy.csrf_trusted_origins(["localhost", "example.com"])
        assert result == ["https://example.com"]
        assert "https://localhost" not in result

    def test_127_0_0_1_filtered(self):
        """Test that 127.0.0.1 is filtered out."""
        result = tls_proxy.csrf_trusted_origins(["127.0.0.1", "example.com"])
        assert result == ["https://example.com"]
        assert "https://127.0.0.1" not in result

    def test_ipv6_loopback_filtered(self):
        """Test that [::1] is filtered out."""
        result = tls_proxy.csrf_trusted_origins(["[::1]", "example.com"])
        assert result == ["https://example.com"]
        assert "https://[::1]" not in result

    def test_all_loopback_addresses(self):
        """Test filtering of all loopback addresses."""
        result = tls_proxy.csrf_trusted_origins(["localhost", "127.0.0.1", "[::1]"])
        assert result == []

    def test_empty_list(self):
        """Test that empty list returns empty list."""
        result = tls_proxy.csrf_trusted_origins([])
        assert result == []

    def test_mixed_loopback_and_public(self):
        """Test mixed loopback and public hosts."""
        result = tls_proxy.csrf_trusted_origins(
            [
                "localhost",
                "example.com",
                "127.0.0.1",
                "api.example.com",
                "[::1]",
            ]
        )
        assert result == [
            "https://example.com",
            "https://api.example.com",
        ]

    def test_wildcard_filtered(self):
        """Test that wildcard * is filtered out (would produce invalid https://* origin)."""
        result = tls_proxy.csrf_trusted_origins(["*", "example.com"])
        assert result == ["https://example.com"]
        assert "https://*" not in result

    def test_subdomain_pattern_preserved(self):
        """Test that Django's .example.com subdomain syntax is preserved."""
        result = tls_proxy.csrf_trusted_origins([".example.com", "api.example.com"])
        assert result == ["https://.example.com", "https://api.example.com"]

    def test_public_hosts_with_ports(self):
        """Test that hosts with port numbers are preserved."""
        result = tls_proxy.csrf_trusted_origins(["example.com:8000", "api.example.com:3000"])
        assert result == [
            "https://example.com:8000",
            "https://api.example.com:3000",
        ]

    def test_ipv6_addresses(self):
        """Test handling of IPv6 addresses."""
        result = tls_proxy.csrf_trusted_origins(["[2001:db8::1]", "example.com"])
        # IPv6 addresses in brackets (other than loopback) should be promoted
        assert "https://[2001:db8::1]" in result
        assert "https://example.com" in result


class TestCloudflareTunnelDefaults:
    """Tests for cloudflare_tunnel_defaults()."""

    def test_required_keys_present(self):
        """Test that all required keys are present."""
        result = tls_proxy.cloudflare_tunnel_defaults(allowed_hosts=["example.com"])
        required_keys = {
            "CSRF_TRUSTED_ORIGINS",
            "SECURE_PROXY_SSL_HEADER",
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
        }
        assert required_keys.issubset(result.keys())

    def test_csrf_trusted_origins_populated(self):
        """Test that CSRF_TRUSTED_ORIGINS is properly set."""
        result = tls_proxy.cloudflare_tunnel_defaults(allowed_hosts=["example.com", "api.example.com"])
        assert result["CSRF_TRUSTED_ORIGINS"] == [
            "https://example.com",
            "https://api.example.com",
        ]

    def test_secure_proxy_ssl_header(self):
        """Test that SECURE_PROXY_SSL_HEADER is set correctly."""
        result = tls_proxy.cloudflare_tunnel_defaults(allowed_hosts=["example.com"])
        assert result["SECURE_PROXY_SSL_HEADER"] == ("HTTP_X_FORWARDED_PROTO", "https")

    def test_secure_cookies_in_debug_mode(self):
        """Test that secure cookies are False in debug mode."""
        result = tls_proxy.cloudflare_tunnel_defaults(
            allowed_hosts=["example.com"],
            debug=True,
        )
        assert result["SESSION_COOKIE_SECURE"] is False
        assert result["CSRF_COOKIE_SECURE"] is False

    def test_secure_cookies_in_production(self):
        """Test that secure cookies are True in production."""
        result = tls_proxy.cloudflare_tunnel_defaults(
            allowed_hosts=["example.com"],
            debug=False,
        )
        assert result["SESSION_COOKIE_SECURE"] is True
        assert result["CSRF_COOKIE_SECURE"] is True

    def test_debug_defaults_to_true(self):
        """Test that debug parameter defaults to True."""
        result = tls_proxy.cloudflare_tunnel_defaults(allowed_hosts=["example.com"])
        # Should have debug=True by default, meaning cookies not secure
        assert result["SESSION_COOKIE_SECURE"] is False
        assert result["CSRF_COOKIE_SECURE"] is False

    def test_empty_allowed_hosts(self):
        """Test that empty allowed_hosts list works."""
        result = tls_proxy.cloudflare_tunnel_defaults(
            allowed_hosts=[],
            debug=False,
        )
        assert result["CSRF_TRUSTED_ORIGINS"] == []
        assert result["SESSION_COOKIE_SECURE"] is True
        assert result["CSRF_COOKIE_SECURE"] is True

    def test_loopback_filtering_in_allowed_hosts(self):
        """Test that loopback addresses are filtered from allowed_hosts."""
        result = tls_proxy.cloudflare_tunnel_defaults(
            allowed_hosts=["localhost", "example.com", "127.0.0.1"],
            debug=False,
        )
        # Only public host should be in CSRF_TRUSTED_ORIGINS
        assert result["CSRF_TRUSTED_ORIGINS"] == ["https://example.com"]
