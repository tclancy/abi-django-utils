"""Tests for abi_django_utils.defaults module."""

from abi_django_utils import defaults


class TestDjangoDefaults:
    """Tests for django_defaults()."""

    def test_returns_dict(self):
        """Test that function returns a dictionary."""
        result = defaults.django_defaults()
        assert isinstance(result, dict)

    def test_required_keys_present(self):
        """Test that all required keys are present."""
        result = defaults.django_defaults()
        required_keys = {
            "LANGUAGE_CODE",
            "TIME_ZONE",
            "USE_I18N",
            "USE_TZ",
            "DEFAULT_AUTO_FIELD",
            "STATICFILES_STORAGE",
            "AUTH_PASSWORD_VALIDATORS",
            "LOGGING",
        }
        assert required_keys.issubset(result.keys())

    def test_language_code_default(self):
        """Test LANGUAGE_CODE setting."""
        result = defaults.django_defaults()
        assert result["LANGUAGE_CODE"] == "en-us"

    def test_timezone_default(self):
        """Test TIME_ZONE setting."""
        result = defaults.django_defaults()
        assert result["TIME_ZONE"] == "UTC"

    def test_auto_field_default(self):
        """Test DEFAULT_AUTO_FIELD setting."""
        result = defaults.django_defaults()
        assert result["DEFAULT_AUTO_FIELD"] == "django.db.models.BigAutoField"

    def test_staticfiles_storage(self):
        """Test STATICFILES_STORAGE setting."""
        result = defaults.django_defaults()
        assert result["STATICFILES_STORAGE"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"

    def test_i18n_enabled(self):
        """Test I18N and TZ settings."""
        result = defaults.django_defaults()
        assert result["USE_I18N"] is True
        assert result["USE_TZ"] is True

    def test_auth_password_validators(self):
        """Test AUTH_PASSWORD_VALIDATORS structure."""
        result = defaults.django_defaults()
        validators = result["AUTH_PASSWORD_VALIDATORS"]
        assert len(validators) == 4
        assert all(isinstance(v, dict) and "NAME" in v for v in validators)

    def test_logging_structure_debug_true(self):
        """Test LOGGING structure with debug=True."""
        result = defaults.django_defaults(debug=True)
        logging_config = result["LOGGING"]

        # Check basic structure
        assert logging_config["version"] == 1
        assert logging_config["disable_existing_loggers"] is False
        assert "handlers" in logging_config
        assert "root" in logging_config
        assert "loggers" in logging_config

        # Check console handler
        assert "console" in logging_config["handlers"]
        assert logging_config["handlers"]["console"]["class"] == "logging.StreamHandler"

        # Check log levels in debug mode
        assert logging_config["root"]["level"] == "DEBUG"
        assert logging_config["loggers"]["django"]["level"] == "DEBUG"
        assert logging_config["loggers"]["django.request"]["level"] == "ERROR"
        assert logging_config["loggers"]["django.request"]["propagate"] is False

    def test_logging_structure_debug_false(self):
        """Test LOGGING structure with debug=False."""
        result = defaults.django_defaults(debug=False)
        logging_config = result["LOGGING"]

        # Check log levels in production mode
        assert logging_config["root"]["level"] == "WARNING"
        assert logging_config["loggers"]["django"]["level"] == "WARNING"
        assert logging_config["loggers"]["django.request"]["level"] == "ERROR"

    def test_debug_parameter_default(self):
        """Test that debug defaults to True."""
        result = defaults.django_defaults()
        # Verify by checking logging levels
        assert result["LOGGING"]["root"]["level"] == "DEBUG"
