"""Opinionated Django settings defaults."""

from __future__ import annotations

import os


def django_defaults(*, debug: bool = False) -> dict:
    """Return a dict of opinionated Django settings defaults.

    Log level respects ``DJANGO_LOG_LEVEL`` env var; falls back to WARNING
    in production and DEBUG in development.
    """
    log_level = os.environ.get("DJANGO_LOG_LEVEL", "DEBUG" if debug else "WARNING")

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
