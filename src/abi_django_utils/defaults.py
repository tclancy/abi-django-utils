"""Opinionated Django settings defaults."""

from __future__ import annotations

import os


def django_defaults(*, debug: bool = False) -> dict:
    """Return a dict of opinionated Django settings defaults.

    Log level respects ``DJANGO_LOG_LEVEL`` env var; falls back to WARNING
    in production and INFO in development. Framework ``DEBUG`` is opt-in
    via ``DJANGO_LOG_LEVEL=DEBUG`` — the default avoids
    ``django.utils.autoreload`` emitting one mtime line per watched file on
    every ``runserver`` reload snapshot (unusable noise once a project pulls
    in debug_toolbar, nplusone, or large vendor SDKs).
    """
    log_level = os.environ.get("DJANGO_LOG_LEVEL", "INFO" if debug else "WARNING")

    settings: dict = {
        "LANGUAGE_CODE": "en-us",
        "TIME_ZONE": "UTC",
        "USE_I18N": True,
        "USE_TZ": True,
        "DEFAULT_AUTO_FIELD": "django.db.models.BigAutoField",
        "STORAGES": {
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
            },
            "staticfiles": {
                "BACKEND": (
                    "django.contrib.staticfiles.storage.StaticFilesStorage"
                    if debug
                    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
                ),
            },
        },
        "AUTH_PASSWORD_VALIDATORS": [
            {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
            {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
            {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
        ],
        "LOGGING": {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                },
            },
            "root": {
                "handlers": ["console"],
                "level": log_level,
            },
            "loggers": {
                "django": {
                    "handlers": ["console"],
                    "level": log_level,
                },
                "django.request": {
                    "handlers": ["console"],
                    "level": "ERROR",
                    "propagate": False,
                },
            },
        },
    }
    return settings
