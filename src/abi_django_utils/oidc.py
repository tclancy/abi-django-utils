"""OIDC integration for Authelia via mozilla-django-oidc.

Ships two things:

* :func:`configure` — a settings-dict builder that maps an Authelia issuer URL
  + client credentials to the ``OIDC_*`` keys ``mozilla-django-oidc`` reads.
* :class:`AutheliaOIDCBackend` — a subclass of
  ``mozilla_django_oidc.auth.OIDCAuthenticationBackend`` that links users by
  case-insensitive email, provisions them on first login, and syncs the
  ``groups`` claim into Django groups under an ``oidc:`` namespace so
  local-only groups (``staff``, per-app permission groups) are never touched.

Additive-only by design: :func:`configure` never emits ``LOGIN_URL``. The
password form at ``/accounts/login/`` stays the default; OIDC is reachable at
``/oidc/authenticate/`` via a link on the login template. Callers that want
OIDC as the default set ``LOGIN_URL = "/oidc/authenticate/"`` explicitly in
their own ``settings.py``.

Install with the ``[oidc]`` extra:

    uv add 'abi-django-utils[oidc] @ git+https://github.com/tclancy/abi-django-utils'

The extra pulls in ``mozilla-django-oidc>=5.0``, which this module imports
at the top of the file. Importing this module without the extra installed
will raise ``ImportError`` — that's intentional; the module has no
meaningful degraded mode.
"""

from __future__ import annotations

from typing import Any

from mozilla_django_oidc.auth import OIDCAuthenticationBackend

OIDC_GROUP_PREFIX = "oidc:"


def _endpoint(issuer_url: str, path: str) -> str:
    return f"{issuer_url.rstrip('/')}{path}"


def configure(
    *,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    scopes: str = "openid email profile groups",
) -> dict[str, Any]:
    """Return the ``OIDC_*`` settings dict for ``mozilla-django-oidc`` v5.x.

    Endpoint paths match Authelia 4.39 defaults. Verify against the running
    ``.well-known/openid-configuration`` at deploy time — Authelia may swap
    ``/api/oidc/*`` for pathless forms in a future minor.

    Never returns ``LOGIN_URL`` — additive-only. The caller's ``settings.py``
    keeps Django's default ``/accounts/login/`` password form authoritative
    unless the app explicitly overrides it.
    """
    return {
        "OIDC_RP_CLIENT_ID": client_id,
        "OIDC_RP_CLIENT_SECRET": client_secret,
        "OIDC_OP_AUTHORIZATION_ENDPOINT": _endpoint(issuer_url, "/api/oidc/authorization"),
        "OIDC_OP_TOKEN_ENDPOINT": _endpoint(issuer_url, "/api/oidc/token"),
        "OIDC_OP_USER_ENDPOINT": _endpoint(issuer_url, "/api/oidc/userinfo"),
        "OIDC_OP_JWKS_ENDPOINT": _endpoint(issuer_url, "/jwks.json"),
        "OIDC_OP_LOGOUT_ENDPOINT": _endpoint(issuer_url, "/api/oidc/logout"),
        "OIDC_RP_SIGN_ALGO": "RS256",
        "OIDC_RP_SCOPES": scopes,
        "OIDC_STORE_ID_TOKEN": True,
        "OIDC_STORE_ACCESS_TOKEN": False,
        "OIDC_CREATE_USER": True,
        "OIDC_USERNAME_ALGO": "abi_django_utils.oidc.username_from_email",
        "OIDC_OP_LOGOUT_URL_METHOD": "abi_django_utils.oidc.rp_initiated_logout_url",
        "AUTHELIA_ISSUER_URL": issuer_url,
    }


def username_from_email(email: str) -> str:
    """Derive a Django username from an email claim.

    Uses the local-part lowercased. Callers can override
    ``OIDC_USERNAME_ALGO`` to something project-specific if needed.
    """
    return email.split("@", 1)[0].lower()


def rp_initiated_logout_url(request) -> str:
    """Return the Authelia logout URL for RP-initiated logout.

    Wired via ``OIDC_OP_LOGOUT_URL_METHOD``. Django's ``logout()`` only kills
    the Django session; without this, the Authelia session cookie
    (``authelia_session``) persists — the user hits any protected view and
    is silently re-authenticated. Passing ``id_token_hint`` per the spec
    lets Authelia end the SSO session cleanly.
    """
    from django.conf import settings

    issuer = getattr(settings, "AUTHELIA_ISSUER_URL", None)
    if not issuer:
        return "/"
    id_token = request.session.get("oidc_id_token")
    logout_url = _endpoint(issuer, "/api/oidc/logout")
    return f"{logout_url}?id_token_hint={id_token}" if id_token else logout_url


def _group_names_from_claims(claims: dict | None) -> set[str]:
    return {f"{OIDC_GROUP_PREFIX}{g}" for g in (claims or {}).get("groups") or []}


class AutheliaOIDCBackend(OIDCAuthenticationBackend):
    """Authelia OIDC auth backend for ``AUTHENTICATION_BACKENDS``.

    Meant to be **appended** to ``AUTHENTICATION_BACKENDS`` after
    ``ModelBackend`` — additive, not replacing. Django tries backends in
    order; ``ModelBackend`` continues to handle password login for
    ``/admin`` and any project login form, while this backend handles the
    OIDC callback path.

    Three overrides:

    * :meth:`filter_users_by_claims` — link by ``email__iexact``.
    * :meth:`create_user` — provision on first login + seed groups.
    * :meth:`update_user` — sync ``oidc:``-namespaced groups on every login.
    """

    def filter_users_by_claims(self, claims):
        email = (claims or {}).get("email")
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def create_user(self, claims):
        email = claims["email"].lower()
        username = claims.get("preferred_username") or username_from_email(email)
        user = self.UserModel.objects.create_user(username=username, email=email)
        self._sync_groups(user, claims)
        return user

    def update_user(self, user, claims):
        canonical = (claims.get("email") or "").lower()
        if canonical and user.email.lower() != canonical:
            user.email = canonical
            user.save(update_fields=["email"])
        self._sync_groups(user, claims)
        return user

    def _sync_groups(self, user, claims):
        """Sync IdP groups → ``oidc:<name>`` Django groups, additively.

        Only groups with the ``oidc:`` prefix are considered under management.
        Locally-created groups without the prefix (``staff``, per-app perm
        groups) are never removed. On group-membership drift the delta is
        applied to the ``oidc:``-namespaced groups only.
        """
        from django.contrib.auth.models import Group

        want = _group_names_from_claims(claims)
        current_oidc = {g.name for g in user.groups.all() if g.name.startswith(OIDC_GROUP_PREFIX)}

        to_add = want - current_oidc
        to_remove = current_oidc - want

        for name in to_add:
            group, _ = Group.objects.get_or_create(name=name)
            user.groups.add(group)

        if to_remove:
            user.groups.remove(*Group.objects.filter(name__in=to_remove))
