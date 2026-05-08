"""Tests for abi_django_utils.defaults module."""

from abi_django_utils import defaults


class TestDjangoDefaults:
    """Tests for django_defaults()."""

    def test_returns_dict(self):
        result = defaults.django_defaults()
        assert isinstance(result, dict)

    def test_required_keys_present(self):
        result = defaults.django_defaults()
        required_keys = {
            "LANGUAGE_CODE",
            "TIME_ZONE",
            "USE_I18N",
            "USE_TZ",
            "DEFAULT_AUTO_FIELD",
            "STORAGES",
            "AUTH_PASSWORD_VALIDATORS",
            "LOGGING",
        }
        assert required_keys.issubset(result.keys())

    def test_language_code_default(self):
        result = defaults.django_defaults()
        assert result["LANGUAGE_CODE"] == "en-us"

    def test_timezone_default(self):
        result = defaults.django_defaults()
        assert result["TIME_ZONE"] == "UTC"

    def test_auto_field_default(self):
        result = defaults.django_defaults()
        assert result["DEFAULT_AUTO_FIELD"] == "django.db.models.BigAutoField"

    def test_storages_debug_uses_staticfiles_storage(self):
        result = defaults.django_defaults(debug=True)
        assert result["STORAGES"]["staticfiles"]["BACKEND"] == "django.contrib.staticfiles.storage.StaticFilesStorage"

    def test_storages_prod_uses_whitenoise(self):
        result = defaults.django_defaults(debug=False)
        assert result["STORAGES"]["staticfiles"]["BACKEND"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"

    def test_storages_has_default_backend(self):
        result = defaults.django_defaults()
        assert result["STORAGES"]["default"]["BACKEND"] == "django.core.files.storage.FileSystemStorage"

    def test_auth_password_validators(self):
        result = defaults.django_defaults()
        validators = result["AUTH_PASSWORD_VALIDATORS"]
        assert len(validators) == 4
        assert all(isinstance(v, dict) and "NAME" in v for v in validators)

    def test_logging_debug_mode(self, monkeypatch):
        monkeypatch.delenv("DJANGO_LOG_LEVEL", raising=False)
        result = defaults.django_defaults(debug=True)
        logging_config = result["LOGGING"]
        assert logging_config["version"] == 1
        assert logging_config["disable_existing_loggers"] is False
        assert logging_config["root"]["level"] == "DEBUG"
        assert logging_config["loggers"]["django"]["level"] == "DEBUG"
        assert logging_config["loggers"]["django.request"]["level"] == "ERROR"
        assert logging_config["loggers"]["django.request"]["propagate"] is False

    def test_logging_production_mode(self, monkeypatch):
        monkeypatch.delenv("DJANGO_LOG_LEVEL", raising=False)
        result = defaults.django_defaults(debug=False)
        logging_config = result["LOGGING"]
        assert logging_config["root"]["level"] == "WARNING"
        assert logging_config["loggers"]["django"]["level"] == "WARNING"

    def test_logging_respects_env_var(self, monkeypatch):
        monkeypatch.setenv("DJANGO_LOG_LEVEL", "ERROR")
        result = defaults.django_defaults(debug=True)
        assert result["LOGGING"]["root"]["level"] == "ERROR"
        assert result["LOGGING"]["loggers"]["django"]["level"] == "ERROR"

    def test_default_is_production_safe(self, monkeypatch):
        monkeypatch.delenv("DJANGO_LOG_LEVEL", raising=False)
        result = defaults.django_defaults()
        assert result["STORAGES"]["staticfiles"]["BACKEND"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"
        assert result["LOGGING"]["root"]["level"] == "WARNING"

    def test_does_not_include_deprecated_staticfiles_storage(self):
        result = defaults.django_defaults()
        assert "STATICFILES_STORAGE" not in result
