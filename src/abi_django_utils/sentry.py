"""Sentry error monitoring initialization."""

from __future__ import annotations

import os


def init(*, debug: bool = True, traces_sample_rate: float = 0.1) -> bool:
    """Initialize Sentry if ``SENTRY_DSN`` is set and not in debug mode.

    Returns True if Sentry was initialized, False otherwise.
    """
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn or debug:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
    )
    return True
