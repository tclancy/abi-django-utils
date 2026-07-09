"""Tests for abi_django_utils.oidc.

Covers configure() (settings-dict builder, endpoint derivation, LOGIN_URL
absence) and AutheliaOIDCBackend (email link, provisioning, group sync).

Backend tests mock at the mozilla-django-oidc parent boundary so no live
HTTP happens (per the mock-at-library-boundary discipline).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User

from abi_django_utils import defaults
from abi_django_utils.oidc import (
    OIDC_GROUP_PREFIX,
    AutheliaOIDCBackend,
    configure,
    rp_initiated_logout_url,
    username_from_email,
)


class TestConfigure:
    """configure() returns the OIDC_* settings dict."""

    def test_returns_client_credentials(self):
        result = configure(
            issuer_url="https://auth.tomclancy.info",
            client_id="heydover",
            client_secret="s3cret",
        )
        assert result["OIDC_RP_CLIENT_ID"] == "heydover"
        assert result["OIDC_RP_CLIENT_SECRET"] == "s3cret"

    def test_derives_all_authelia_endpoints(self):
        result = configure(
            issuer_url="https://auth.tomclancy.info",
            client_id="app",
            client_secret="x",
        )
        assert result["OIDC_OP_AUTHORIZATION_ENDPOINT"] == "https://auth.tomclancy.info/api/oidc/authorization"
        assert result["OIDC_OP_TOKEN_ENDPOINT"] == "https://auth.tomclancy.info/api/oidc/token"
        assert result["OIDC_OP_USER_ENDPOINT"] == "https://auth.tomclancy.info/api/oidc/userinfo"
        assert result["OIDC_OP_JWKS_ENDPOINT"] == "https://auth.tomclancy.info/jwks.json"
        assert result["OIDC_OP_LOGOUT_ENDPOINT"] == "https://auth.tomclancy.info/api/oidc/logout"

    def test_strips_trailing_slash_from_issuer(self):
        # Authelia's discovery emits without trailing slash; guard against
        # a caller who pastes the URL from a browser.
        with_slash = configure(
            issuer_url="https://auth.tomclancy.info/",
            client_id="app",
            client_secret="x",
        )
        without_slash = configure(
            issuer_url="https://auth.tomclancy.info",
            client_id="app",
            client_secret="x",
        )
        assert with_slash["OIDC_OP_TOKEN_ENDPOINT"] == without_slash["OIDC_OP_TOKEN_ENDPOINT"]

    def test_sign_algo_is_rs256_not_hs256(self):
        # mozilla-django-oidc defaults to HS256 (client_secret as signing
        # key). Authelia signs with RSA. Wrong algo silently fails —
        # regression-lock the correct value.
        result = configure(issuer_url="https://x", client_id="a", client_secret="b")
        assert result["OIDC_RP_SIGN_ALGO"] == "RS256"

    def test_scopes_default_includes_groups(self):
        result = configure(issuer_url="https://x", client_id="a", client_secret="b")
        assert "groups" in result["OIDC_RP_SCOPES"]

    def test_scopes_customizable(self):
        result = configure(
            issuer_url="https://x",
            client_id="a",
            client_secret="b",
            scopes="openid email",
        )
        assert result["OIDC_RP_SCOPES"] == "openid email"

    def test_does_not_set_login_url(self):
        # Additive-only guarantee: OIDC never becomes the default login by
        # importing this library. A caller who wants that sets LOGIN_URL
        # themselves in settings.py.
        result = configure(issuer_url="https://x", client_id="a", client_secret="b")
        assert "LOGIN_URL" not in result

    def test_stores_id_token_for_rp_logout(self):
        # Without OIDC_STORE_ID_TOKEN=True, rp_initiated_logout_url can't
        # emit the id_token_hint and Authelia session persists.
        result = configure(issuer_url="https://x", client_id="a", client_secret="b")
        assert result["OIDC_STORE_ID_TOKEN"] is True

    def test_provides_username_algo_hook(self):
        result = configure(issuer_url="https://x", client_id="a", client_secret="b")
        assert result["OIDC_USERNAME_ALGO"] == "abi_django_utils.oidc.username_from_email"

    def test_provides_logout_url_method_hook(self):
        result = configure(issuer_url="https://x", client_id="a", client_secret="b")
        assert result["OIDC_OP_LOGOUT_URL_METHOD"] == "abi_django_utils.oidc.rp_initiated_logout_url"

    def test_stashes_issuer_for_logout_helper(self):
        # rp_initiated_logout_url() reads settings.AUTHELIA_ISSUER_URL.
        result = configure(
            issuer_url="https://auth.tomclancy.info",
            client_id="a",
            client_secret="b",
        )
        assert result["AUTHELIA_ISSUER_URL"] == "https://auth.tomclancy.info"


class TestComposability:
    """oidc.configure() merges cleanly with defaults.django_defaults()."""

    def test_no_key_collision_with_django_defaults(self):
        base = defaults.django_defaults(debug=False)
        oidc_dict = configure(issuer_url="https://x", client_id="a", client_secret="b")
        overlap = set(base) & set(oidc_dict)
        assert overlap == set(), f"Unexpected key overlap: {overlap}"

    def test_merge_produces_both_families(self):
        merged = defaults.django_defaults(debug=False) | configure(
            issuer_url="https://x", client_id="a", client_secret="b"
        )
        assert "LOGGING" in merged  # from defaults
        assert "OIDC_RP_CLIENT_ID" in merged  # from oidc


class TestUsernameFromEmail:
    def test_local_part_lowercased(self):
        assert username_from_email("Tclancy@gmail.com") == "tclancy"

    def test_dotted_local_part_preserved(self):
        assert username_from_email("tom.clancy@example.org") == "tom.clancy"

    def test_no_at_returns_input_lowercased(self):
        # Defensive — shouldn't happen in practice (OIDC email claim is
        # required to be an email), but don't crash the login.
        assert username_from_email("weird") == "weird"


class TestRPInitiatedLogoutURL:
    def _mock_request(self, id_token=None):
        class _Session(dict):
            pass

        class _Req:
            session = _Session()

        req = _Req()
        if id_token is not None:
            req.session["oidc_id_token"] = id_token
        return req

    def test_appends_id_token_hint_when_present(self):
        req = self._mock_request(id_token="abc.def.ghi")
        url = rp_initiated_logout_url(req)
        assert url == "https://auth.example.com/api/oidc/logout?id_token_hint=abc.def.ghi"

    def test_falls_back_to_bare_logout_when_no_token(self):
        req = self._mock_request(id_token=None)
        url = rp_initiated_logout_url(req)
        assert url == "https://auth.example.com/api/oidc/logout"

    def test_returns_root_when_issuer_not_configured(self, settings):
        # If AUTHELIA_ISSUER_URL is missing (misconfiguration), don't
        # crash the logout flow — just send the user home.
        settings.AUTHELIA_ISSUER_URL = None
        req = self._mock_request(id_token="x")
        url = rp_initiated_logout_url(req)
        assert url == "/"


@pytest.mark.django_db
class TestAutheliaOIDCBackend:
    """AutheliaOIDCBackend — user linking, provisioning, group sync.

    Bypasses the parent __init__ (which does discovery HTTP) by patching
    it out at instantiation. All logic under test is in the overrides.
    """

    def _backend(self):
        with patch.object(AutheliaOIDCBackend.__bases__[0], "__init__", return_value=None):
            backend = AutheliaOIDCBackend()
        backend.UserModel = User
        return backend

    def test_filter_users_matches_by_iexact_email(self):
        User.objects.create_user(username="tom", email="tom@example.com")
        backend = self._backend()

        matches = backend.filter_users_by_claims({"email": "TOM@example.com"})
        assert matches.count() == 1
        assert matches.first().username == "tom"

    def test_filter_users_empty_when_email_missing(self):
        backend = self._backend()
        assert backend.filter_users_by_claims({}).count() == 0
        assert backend.filter_users_by_claims({"email": None}).count() == 0

    def test_filter_users_empty_when_claims_none(self):
        backend = self._backend()
        assert backend.filter_users_by_claims(None).count() == 0

    def test_create_user_uses_preferred_username_when_present(self):
        backend = self._backend()
        user = backend.create_user({"email": "New@example.com", "preferred_username": "newuser"})
        assert user.username == "newuser"
        assert user.email == "new@example.com"  # canonicalized to lowercase

    def test_create_user_derives_username_from_email(self):
        backend = self._backend()
        user = backend.create_user({"email": "Someone@example.com"})
        assert user.username == "someone"

    def test_create_user_seeds_oidc_groups(self):
        backend = self._backend()
        user = backend.create_user({"email": "a@b.c", "groups": ["admins", "editors"]})
        names = set(user.groups.values_list("name", flat=True))
        assert names == {"oidc:admins", "oidc:editors"}

    def test_update_user_syncs_groups_additively(self):
        # Baseline: user has one OIDC group (oidc:admins) and one
        # local-only group (staff). IdP now sends only 'editors'.
        # Expected: oidc:admins removed, oidc:editors added, staff untouched.
        user = User.objects.create_user(username="linked", email="l@x.com")
        Group.objects.create(name="oidc:admins")
        staff, _ = Group.objects.get_or_create(name="staff")
        oidc_admins = Group.objects.get(name="oidc:admins")
        user.groups.add(oidc_admins, staff)

        backend = self._backend()
        backend.update_user(user, {"email": "l@x.com", "groups": ["editors"]})

        names = set(user.groups.values_list("name", flat=True))
        assert names == {"oidc:editors", "staff"}

    def test_update_user_leaves_local_only_groups_when_idp_sends_no_groups(self):
        # Regression for gotcha 4 — group sync must NOT clear local groups
        # when the IdP claim is empty/missing.
        user = User.objects.create_user(username="local", email="u@x.com")
        staff, _ = Group.objects.get_or_create(name="staff")
        editors, _ = Group.objects.get_or_create(name="oidc:editors")
        user.groups.add(staff, editors)

        backend = self._backend()
        backend.update_user(user, {"email": "u@x.com"})  # no 'groups' key

        names = set(user.groups.values_list("name", flat=True))
        # Local staff kept; oidc:editors removed because IdP claim absent.
        assert names == {"staff"}

    def test_update_user_does_not_rewrite_case_only_email_change(self):
        # Gotcha 3 — matching is case-insensitive (email__iexact) so a
        # case-only difference between DB and IdP is a semantic no-op.
        # Rewriting the user's stored email on every login just because
        # of case is surprising (invalidates a user's chosen display form).
        user = User.objects.create_user(username="c", email="Old@example.com")
        backend = self._backend()
        backend.update_user(user, {"email": "old@example.com"})

        user.refresh_from_db()
        assert user.email == "Old@example.com"

    def test_update_user_rewrites_email_on_real_change(self):
        # But if the IdP genuinely changed the email address (not just
        # case), do sync — otherwise the next login won't find this user
        # by the new address.
        user = User.objects.create_user(username="c", email="old@example.com")
        backend = self._backend()
        backend.update_user(user, {"email": "new@example.com"})

        user.refresh_from_db()
        assert user.email == "new@example.com"

    def test_update_user_does_not_touch_email_when_unchanged(self):
        user = User.objects.create_user(username="c", email="stable@example.com")
        backend = self._backend()

        with patch.object(User, "save") as mock_save:
            backend.update_user(user, {"email": "stable@example.com", "groups": []})
            # save() should NOT be called for the email path since it didn't change.
            # (Group sync uses .add/.remove on the related manager, not user.save.)
            mock_save.assert_not_called()


class TestGroupNamespaceFence:
    """The oidc: prefix isolates library-managed groups from local groups."""

    def test_prefix_is_stable(self):
        # Change to this prefix would silently orphan every existing
        # oidc:-prefixed group across the fleet. Regression-lock.
        assert OIDC_GROUP_PREFIX == "oidc:"
