"""TLS proxy (Cloudflare Tunnel) settings helpers."""

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def csrf_trusted_origins(hosts: list[str]) -> list[str]:
    """Promote public ALLOWED_HOSTS to https:// origins for CSRF."""
    return [f"https://{h}" for h in hosts if h not in _LOOPBACK_HOSTS]


def cloudflare_tunnel_defaults(*, allowed_hosts: list[str], debug: bool = True) -> dict:
    """Settings for running behind a TLS-terminating reverse proxy."""
    return {
        "CSRF_TRUSTED_ORIGINS": csrf_trusted_origins(allowed_hosts),
        "SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https"),
        "SESSION_COOKIE_SECURE": not debug,
        "CSRF_COOKIE_SECURE": not debug,
    }
