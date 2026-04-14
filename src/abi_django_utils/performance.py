"""
Performance monitoring utilities for Django projects.

Standard pattern for all Tom Clancy Django projects:
- Django Debug Toolbar in DEBUG mode
- nplusone N+1 detection in DEBUG mode, warning in production
- Sentry performance tracing (via sentry-sdk[django])

Usage in settings.py:
    from abi_django_utils.performance import configure_debug_apps, configure_debug_middleware

    INSTALLED_APPS = [...] + (configure_debug_apps() if DEBUG else [])
    MIDDLEWARE = configure_debug_middleware(MIDDLEWARE, DEBUG)

    # N+1 detection
    NPLUSONE_RAISE = DEBUG
    NPLUSONE_LOGGER = "nplusone.logger"
    NPLUSONE_LOG_LEVEL = "WARNING"

    # Django Debug Toolbar: only show to localhost
    INTERNAL_IPS = ["127.0.0.1"]

Usage in urls.py:
    from abi_django_utils.performance import debug_urls
    if settings.DEBUG:
        urlpatterns += debug_urls()
"""

from typing import Any


def configure_debug_apps() -> list[str]:
    """Apps to add to INSTALLED_APPS when DEBUG=True."""
    return [
        "debug_toolbar",
        "nplusone.apps.NPlusOneApp",
    ]


def configure_debug_middleware(middleware: list[str], debug: bool) -> list[str]:
    """
    Add debug middleware to an existing MIDDLEWARE list.

    DjDT must come early (index 1, after SecurityMiddleware).
    nplusone must come last (after all response processing).
    """
    if not debug:
        return middleware
    result = list(middleware)
    result.insert(1, "debug_toolbar.middleware.DebugToolbarMiddleware")
    result.append("nplusone.ext.django.NPlusOneMiddleware")
    return result


def debug_urls() -> list[Any]:
    """URL patterns to add when DEBUG=True. Call from urls.py."""
    from django.urls import include, path
    import debug_toolbar

    return [path("__debug__/", include(debug_toolbar.urls))]


# Standard nplusone settings to add to settings.py when DEBUG=True
NPLUSONE_SETTINGS: dict[str, Any] = {
    "NPLUSONE_RAISE": True,  # Raises NPlusOneError — use with DEBUG check: NPLUSONE_RAISE = DEBUG
    "NPLUSONE_LOGGER": "nplusone.logger",
    "NPLUSONE_LOG_LEVEL": "WARNING",
}
