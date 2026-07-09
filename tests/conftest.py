# pytest configuration — shared fixtures live here.

import django
from django.conf import settings


def pytest_configure():
    """Minimal Django setup for the oidc backend tests.

    The backend needs Django's ORM + auth machinery imported. We use
    an in-memory SQLite DB and a trivial settings module so no fixtures
    or migrations file need to ship in the library repo.
    """
    if settings.configured:
        return

    settings.configure(
        DEBUG=False,
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "mozilla_django_oidc",
        ],
        AUTH_USER_MODEL="auth.User",
        MIDDLEWARE=[],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        SECRET_KEY="test-only-not-a-secret",
        AUTHELIA_ISSUER_URL="https://auth.example.com",
        # mozilla-django-oidc parent needs these at instantiation; unused by the tests.
        OIDC_RP_CLIENT_ID="test-client",
        OIDC_RP_CLIENT_SECRET="test-secret",
        OIDC_OP_AUTHORIZATION_ENDPOINT="https://auth.example.com/api/oidc/authorization",
        OIDC_OP_TOKEN_ENDPOINT="https://auth.example.com/api/oidc/token",
        OIDC_OP_USER_ENDPOINT="https://auth.example.com/api/oidc/userinfo",
        OIDC_OP_JWKS_ENDPOINT="https://auth.example.com/jwks.json",
        OIDC_RP_SIGN_ALGO="RS256",
    )
    django.setup()

    from django.core.management import call_command

    call_command("migrate", "--run-syncdb", verbosity=0)
