"""Environment variable helpers for Django configuration."""

import builtins
import os


def bool(name: str, default: bool = False) -> bool:
    """Read an env var as a boolean. Truthy: '1', 'true', 'yes', 'on'."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def list(name: str, default: list[str] | None = None) -> list[str]:
    """Read a comma-separated env var as a list. Strips whitespace, drops empties."""
    raw = os.environ.get(name, "")
    if not raw:
        return builtins.list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]
