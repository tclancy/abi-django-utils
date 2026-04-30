"""Opinionated Django settings defaults."""


def django_defaults(*, debug: bool = True) -> dict:
    """Return a dict of opinionated Django settings defaults."""
    return {
        "LANGUAGE_CODE": "en-us",
        "TIME_ZONE": "UTC",
        "USE_I18N": True,
        "USE_TZ": True,
        "DEFAULT_AUTO_FIELD": "django.db.models.BigAutoField",
        "STATICFILES_STORAGE": "whitenoise.storage.CompressedManifestStaticFilesStorage",
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
                "level": "DEBUG" if debug else "WARNING",
            },
            "loggers": {
                "django": {
                    "handlers": ["console"],
                    "level": "DEBUG" if debug else "WARNING",
                },
                "django.request": {
                    "handlers": ["console"],
                    "level": "ERROR",
                    "propagate": False,
                },
            },
        },
    }
