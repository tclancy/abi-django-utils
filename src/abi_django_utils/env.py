"""Environment variable helpers for Django configuration."""

from __future__ import annotations

import builtins
import os


def str(name: builtins.str, default: builtins.str = "") -> builtins.str:
    """Read an env var as a string."""
    return os.environ.get(name, default)


def bool(name: builtins.str, default: builtins.bool = False) -> builtins.bool:
    """Read an env var as a boolean. Truthy: '1', 'true', 'yes', 'on'."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def int(name: builtins.str, default: builtins.int = 0) -> builtins.int:
    """Read an env var as an integer."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return builtins.int(raw.strip())


def list(name: builtins.str, default: builtins.list[builtins.str] | None = None) -> builtins.list[builtins.str]:
    """Read a comma-separated env var as a list. Strips whitespace, drops empties."""
    raw = os.environ.get(name, "")
    if not raw:
        return builtins.list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]
