"""Tests for abi_django_utils.brute_force module.

Two of these tests are not about this library's output at all -- they drive
``axes.helpers.get_client_ip_address`` and assert which IP django-axes resolves
a request to, with and without the precedence order this module ships. They are
here because the whole point of the helper is a setting whose value is invisible
in its own assertion: ``AXES_IPWARE_META_PRECEDENCE_ORDER`` is a list of strings
that django-axes silently ignores when ``django-ipware`` is absent, and whose
own default resolves every tunnelled request to the proxy. A test that only
compares the list to itself cannot tell a live pin from a dead one.
"""

import importlib.util

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.utils.module_loading import import_string

from abi_django_utils import brute_force

#: The address a Cloudflare-tunnelled request arrives from, quoted into
#: abi-django-utils#16 from tclancy/homelab#572 and not re-measured here. These
#: tests only need it to differ from the other two.
PROXY_IP = "172.28.0.1"
#: What Cloudflare puts in ``CF-Connecting-IP`` -- the real client.
CLIENT_IP = "198.51.100.7"
#: What a client can put in ``X-Forwarded-For`` for free.
SPOOFED_IP = "203.0.113.9"


@pytest.fixture
def tunnelled_request():
    """A login POST as it arrives through the Cloudflare tunnel.

    All three headers are populated and all three differ, so whichever one the
    resolver prefers is identifiable from the address alone.
    """
    return RequestFactory().post(
        "/admin/login/",
        REMOTE_ADDR=PROXY_IP,
        HTTP_X_FORWARDED_FOR=SPOOFED_IP,
        HTTP_CF_CONNECTING_IP=CLIENT_IP,
    )


class TestIpwareInstalled:
    """Tests for ipware_installed()."""

    def test_true_in_this_environment(self):
        """The dev group installs django-axes[ipware], so the probe says yes.

        This is the arm that cannot be faked by a monkeypatch: it reads the
        real interpreter. If the extra is ever dropped from the dev group this
        goes red, which is the only warning that the two behavioural tests
        below have stopped testing what they claim to.
        """
        assert brute_force.ipware_installed() is True

    def test_agrees_with_axes_own_probe(self):
        """The probe must answer the same question django-axes asks itself.

        django-axes decides whether to consult ipware from its own
        ``IPWARE_INSTALLED``, computed from ``import ipware.ip``. If this
        module probed a different name the raise below would fire on the wrong
        condition.
        """
        from axes.helpers import IPWARE_INSTALLED

        assert brute_force.ipware_installed() is IPWARE_INSTALLED

    def test_false_when_the_module_cannot_be_found(self, monkeypatch):
        """The answer has to come from the interpreter, not from a constant.

        A probe hardcoded to ``True`` satisfies both tests above in every
        environment this suite can run in, because the dev group installs
        ipware. A mutation round confirmed that: ``return True`` survived until
        this arm existed. Patching the lookup the probe actually performs is
        what separates "derived" from "asserted".
        """
        real_find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *args, **kwargs: None if name == "ipware" else real_find_spec(name, *args, **kwargs),
        )
        assert brute_force.ipware_installed() is False


class TestDottedPathsResolve:
    """The module's dotted strings must name things that exist."""

    def test_axes_backend_imports(self):
        assert import_string(brute_force.AXES_BACKEND) is not None

    def test_axes_middleware_imports(self):
        assert import_string(brute_force.AXES_MIDDLEWARE) is not None

    def test_model_backend_imports(self):
        assert import_string(brute_force.DJANGO_MODEL_BACKEND) is not None


class TestWithAxesBackend:
    """Tests for with_axes_backend()."""

    def test_axes_backend_is_first(self):
        result = brute_force.with_axes_backend()
        assert result == [
            "axes.backends.AxesStandaloneBackend",
            "django.contrib.auth.backends.ModelBackend",
        ]

    def test_caller_backends_keep_their_order(self):
        """Two differing inputs, so the result cannot be a hardcoded list."""
        result = brute_force.with_axes_backend(["one.Backend", "two.Backend"])
        assert result == [
            "axes.backends.AxesStandaloneBackend",
            "one.Backend",
            "two.Backend",
        ]
        other = brute_force.with_axes_backend(["two.Backend", "one.Backend"])
        assert other == [
            "axes.backends.AxesStandaloneBackend",
            "two.Backend",
            "one.Backend",
        ]

    def test_idempotent(self):
        once = brute_force.with_axes_backend()
        assert brute_force.with_axes_backend(once) == once

    def test_does_not_mutate_the_argument(self):
        backends = ["one.Backend"]
        brute_force.with_axes_backend(backends)
        assert backends == ["one.Backend"]


class TestWithAxesApp:
    """Tests for with_axes_app()."""

    def test_appends_axes(self):
        result = brute_force.with_axes_app(["django.contrib.auth"])
        assert result == ["django.contrib.auth", "axes"]

    def test_preserves_existing_order(self):
        result = brute_force.with_axes_app(["b", "a"])
        assert result == ["b", "a", "axes"]

    def test_idempotent(self):
        result = brute_force.with_axes_app(["django.contrib.auth", "axes"])
        assert result == ["django.contrib.auth", "axes"]

    def test_does_not_mutate_the_argument(self):
        apps = ["django.contrib.auth"]
        brute_force.with_axes_app(apps)
        assert apps == ["django.contrib.auth"]


class TestWithAxesMiddleware:
    """Tests for with_axes_middleware()."""

    def test_appends_at_the_end(self):
        result = brute_force.with_axes_middleware(
            [
                "django.middleware.security.SecurityMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )
        assert result[-1] == "axes.middleware.AxesMiddleware"
        assert result == [
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "axes.middleware.AxesMiddleware",
        ]

    def test_idempotent_and_still_last(self):
        once = brute_force.with_axes_middleware(["a.M"])
        twice = brute_force.with_axes_middleware(once)
        assert twice == once
        assert twice[-1] == "axes.middleware.AxesMiddleware"

    def test_moves_an_already_present_entry_to_the_end(self):
        """Already-present is not enough -- it has to end up last.

        An app that put the middleware mid-list gets it moved rather than
        silently left where it was, because wrapping the view is the whole
        reason this entry exists.
        """
        result = brute_force.with_axes_middleware(["a.M", "axes.middleware.AxesMiddleware", "b.M"])
        assert result == ["a.M", "b.M", "axes.middleware.AxesMiddleware"]

    def test_does_not_mutate_the_argument(self):
        middleware = ["a.M"]
        brute_force.with_axes_middleware(middleware)
        assert middleware == ["a.M"]


class TestAxesDefaults:
    """Tests for axes_defaults()."""

    def test_required_keys_present(self):
        result = brute_force.axes_defaults()
        assert set(result) == {
            "AUTHENTICATION_BACKENDS",
            "AXES_FAILURE_LIMIT",
            "AXES_COOLOFF_TIME",
            "AXES_LOCKOUT_PARAMETERS",
            "AXES_RESET_ON_SUCCESS",
            "AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT",
            "AXES_ONLY_ADMIN_SITE",
            "AXES_IPWARE_META_PRECEDENCE_ORDER",
        }

    def test_authentication_backends_order_is_asserted_not_membership(self):
        result = brute_force.axes_defaults()
        assert result["AUTHENTICATION_BACKENDS"] == [
            "axes.backends.AxesStandaloneBackend",
            "django.contrib.auth.backends.ModelBackend",
        ]

    def test_precedence_order_puts_cf_connecting_ip_first(self):
        result = brute_force.axes_defaults()
        assert result["AXES_IPWARE_META_PRECEDENCE_ORDER"] == [
            "HTTP_CF_CONNECTING_IP",
            "HTTP_X_FORWARDED_FOR",
            "REMOTE_ADDR",
        ]

    def test_matches_the_heydover_reference_values(self):
        """The values are heydover's, which is the longest-running of the five."""
        result = brute_force.axes_defaults()
        assert result["AXES_FAILURE_LIMIT"] == 5
        assert result["AXES_COOLOFF_TIME"] == 1
        assert result["AXES_LOCKOUT_PARAMETERS"] == [["ip_address"]]
        assert result["AXES_RESET_ON_SUCCESS"] is True

    def test_only_admin_site_defaults_to_true(self):
        assert brute_force.axes_defaults()["AXES_ONLY_ADMIN_SITE"] is True

    def test_only_admin_site_can_be_opted_out_of(self):
        """An app with a non-admin login surface widens the cover deliberately."""
        result = brute_force.axes_defaults(only_admin_site=False)
        assert result["AXES_ONLY_ADMIN_SITE"] is False

    def test_failure_limit_and_cooloff_are_tunable(self):
        result = brute_force.axes_defaults(failure_limit=3, cooloff_hours=24)
        assert result["AXES_FAILURE_LIMIT"] == 3
        assert result["AXES_COOLOFF_TIME"] == 24

    def test_caller_backends_are_carried_through(self):
        result = brute_force.axes_defaults(backends=["one.Backend"])
        assert result["AUTHENTICATION_BACKENDS"] == [
            "axes.backends.AxesStandaloneBackend",
            "one.Backend",
        ]

    def test_raises_when_ipware_is_absent(self, monkeypatch):
        """Refuse to emit a setting django-axes would ignore.

        Without django-ipware the precedence order is dead config, and the app
        reads as protected while every tunnelled request resolves to the proxy.
        """
        monkeypatch.setattr(brute_force, "ipware_installed", lambda: False)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            brute_force.axes_defaults()
        assert "django-axes[ipware]" in str(excinfo.value)

    def test_proxy_aware_false_omits_the_precedence_order(self):
        """The escape hatch drops the key rather than shipping it inert."""
        result = brute_force.axes_defaults(proxy_aware=False)
        assert "AXES_IPWARE_META_PRECEDENCE_ORDER" not in result
        assert result["AXES_FAILURE_LIMIT"] == 5

    def test_proxy_aware_false_does_not_raise_without_ipware(self, monkeypatch):
        monkeypatch.setattr(brute_force, "ipware_installed", lambda: False)
        result = brute_force.axes_defaults(proxy_aware=False)
        assert "AXES_IPWARE_META_PRECEDENCE_ORDER" not in result


class TestAxesResolvesTheRealClient:
    """What django-axes actually does with the setting this module ships."""

    def test_cf_connecting_ip_wins_over_a_spoofed_forwarded_for(self, tunnelled_request):
        """The pin is live: axes keys the lockout on the Cloudflare header.

        Both the spoofable header and the proxy's own address are present and
        both differ from the answer, so this passes for one reason only.
        """
        from axes.helpers import get_client_ip_address

        with override_settings(
            AXES_IPWARE_META_PRECEDENCE_ORDER=brute_force.axes_defaults()["AXES_IPWARE_META_PRECEDENCE_ORDER"]
        ):
            assert get_client_ip_address(tunnelled_request) == CLIENT_IP

    def test_without_the_pin_every_tunnelled_client_shares_the_proxy_ip(self, tunnelled_request):
        """The counterfactual, and the reason the helper refuses to ship inert.

        django-axes defaults ``AXES_IPWARE_META_PRECEDENCE_ORDER`` to
        ``("REMOTE_ADDR",)``, so an unpinned app resolves every tunnelled
        request to the gateway. With ``AXES_LOCKOUT_PARAMETERS`` keyed on
        ``ip_address`` that is a single shared bucket: five failures from
        anyone lock out everyone. If django-axes ever changes this default,
        this test is how we find out.
        """
        from axes.helpers import get_client_ip_address

        with override_settings(AXES_IPWARE_META_PRECEDENCE_ORDER=("REMOTE_ADDR",)):
            assert get_client_ip_address(tunnelled_request) == PROXY_IP

    def test_without_ipware_the_pin_is_not_read_at_all(self, tunnelled_request):
        """The upstream fact the ImproperlyConfigured raise rests on.

        Everything else here runs with ipware installed, which is the only
        environment this suite can be in -- so the claim that the precedence
        order is *ignored* without it had nothing behind it. django-axes
        exposes the seam as ``use_ipware``, taken from its own
        ``IPWARE_INSTALLED`` when not passed. If a release ever started
        honouring the setting independently, the raise would become pure
        friction and this is what would say so.
        """
        from axes.helpers import get_client_ip_address

        with override_settings(AXES_IPWARE_META_PRECEDENCE_ORDER=brute_force.CLOUDFLARE_META_PRECEDENCE_ORDER):
            assert get_client_ip_address(tunnelled_request, use_ipware=False) == PROXY_IP

    def test_a_direct_request_can_choose_its_own_lockout_key(self):
        """The pin's limit, pinned so it is not mistaken for a guarantee.

        ``CF-Connecting-IP`` is unspoofable only on traffic that transits
        Cloudflare. A request that reaches Django by another route sets it
        itself and gets a fresh lockout key per attempt. This is a property of
        the deployment, not a defect in the helper, and it is asserted here so
        the README's claim about it stays true.
        """
        from axes.helpers import get_client_ip_address

        direct = RequestFactory().post(
            "/admin/login/",
            REMOTE_ADDR="192.168.1.50",
            HTTP_CF_CONNECTING_IP="1.2.3.4",
        )
        with override_settings(AXES_IPWARE_META_PRECEDENCE_ORDER=brute_force.CLOUDFLARE_META_PRECEDENCE_ORDER):
            assert get_client_ip_address(direct) == "1.2.3.4"

    def test_axes_own_default_is_remote_addr_only(self):
        """Pinned here because the ticket that asked for this said otherwise.

        homelab#572 and abi-django-utils#16 both describe the unpinned default
        as reading a client-supplied ``X-Forwarded-For`` ahead of
        ``REMOTE_ADDR``. It does not; it reads ``REMOTE_ADDR`` alone.
        """
        from axes.conf import settings as axes_settings

        assert axes_settings.AXES_IPWARE_META_PRECEDENCE_ORDER == ("REMOTE_ADDR",)


class TestCoolOffResetsOnFailure:
    """Tests for the ``AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT`` pin.

    abi-django-utils#22. The value is deliberately the one django-axes already
    defaults to, which makes every assertion below easy to write and easy to
    write *uselessly*: an assertion that the emitted value is ``True`` passes
    identically whether the helper pins it or the key is absent and upstream
    supplied it. So the membership arm (``test_required_keys_present``) and
    ``test_the_upstream_default_is_true`` are both load-bearing here -- the
    first says the key is *ours*, the second says what we would be inheriting
    if it were not.
    """

    def test_defaults_to_the_behaviour_every_app_runs_today(self):
        """``True`` is behaviour-preserving, not a preference.

        No consuming app pinned this key, so all of them inherited ``True``
        from django-axes. ``recordclub`` -- the only app consuming this helper
        -- left it there deliberately and pinned it with a behavioural test
        (``TheCooloffSlidesOnEveryRequestDuringLockoutTest``). A helper that
        emitted ``False`` would have flipped a security control's behaviour in
        the app that asked for this ticket.
        """
        assert brute_force.axes_defaults()["AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT"] is True

    def test_a_fixed_window_can_be_asked_for(self):
        """The escape hatch, in the shape ``proxy_aware`` established.

        An app that wants an hour from the failure that crossed the limit --
        rather than an hour of silence -- asks for it in one keyword instead of
        a seventh inlined ``AXES_`` assignment, which is the drift #16 exists
        to stop.
        """
        result = brute_force.axes_defaults(cool_off_resets_on_failure=False)
        assert result["AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT"] is False

    def test_the_other_values_are_unaffected_by_the_flag(self):
        """Two differing inputs, so neither result can be a hardcoded dict."""
        on = brute_force.axes_defaults(cool_off_resets_on_failure=True)
        off = brute_force.axes_defaults(cool_off_resets_on_failure=False)
        del on["AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT"]
        del off["AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT"]
        assert on == off

    def test_the_upstream_default_is_true(self):
        """What an unpinned app inherits, read from axes rather than asserted.

        This is the counterfactual the pin exists for. If a django-axes release
        ever flips this default, the pin above keeps every consuming app on
        today's behaviour and this test is what says the inheritance changed --
        which is the whole argument for pinning a key whose value already
        matches the default.
        """
        from axes.conf import settings as axes_settings

        assert axes_settings.AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT is True

    def test_the_setting_is_read_by_the_handler_this_helper_targets(self):
        """The pin is live, not a list of strings axes never looks at.

        ``AXES_IPWARE_META_PRECEDENCE_ORDER`` taught this module that a pinned
        setting can be inert -- axes reads it only when ipware is installed.
        The same question has to be asked of this key, and the answer is a
        different one: ``axes.handlers.database`` consults it unconditionally
        in ``user_login_failed``, which is the handler ``AXES_HANDLER`` selects
        by default. Asserted against the real source so that a release which
        moved or dropped the read is caught here rather than in production.
        """
        import inspect

        from axes.handlers.database import AxesDatabaseHandler

        source = inspect.getsource(AxesDatabaseHandler.user_login_failed)
        assert "AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT" in source


class TestTheRefusalIsA429AndThePositionIsNotLoadBearing:
    """The two claims ``with_axes_middleware``'s docstring used to get wrong.

    abi-django-utils#22, second item. Both are upstream facts rather than
    properties of this module's output, which is exactly why they were wrong
    for as long as they were: nothing in the suite drove them.
    """

    def test_the_lockout_status_is_429_not_403(self):
        """The docstring said "the 403 the client sees". axes says 429.

        Pinned rather than corrected-and-forgotten, because the next person to
        write "403" into a changelog or a runbook will be reading the same
        default this reads.
        """
        from axes.conf import settings as axes_settings

        assert axes_settings.AXES_HTTP_RESPONSE_CODE == 429

    def test_the_flag_is_read_after_the_view_so_position_cannot_matter(self):
        """Drive the real middleware: it is response-phase only.

        ``get_response`` here stands in for the view, and sets the flag the way
        ``AxesStandaloneBackend`` does during ``authenticate()``. The
        middleware still returns the lockout refusal, which it could only do by
        reading the flag *after* calling ``get_response`` -- and a middleware
        that reads the request after the view has run wraps that view from any
        index in ``MIDDLEWARE``.

        This is the arm that makes "presence, not position" a measurement here
        rather than a quotation of tclancy/itemshop#372.
        """
        from axes.middleware import AxesMiddleware

        def view(request):
            request.axes_locked_out = True
            request.axes_credentials = {"username": "someone"}
            return HttpResponse("the login form", status=200)

        request = RequestFactory().post("/admin/login/", REMOTE_ADDR=CLIENT_IP)
        with override_settings(AXES_ENABLED=True):
            response = AxesMiddleware(view)(request)

        assert response.status_code == 429

    def test_an_unflagged_request_passes_straight_through(self):
        """The negative control: the middleware is not a blanket refusal.

        Without this, the test above passes for a middleware that returns 429
        unconditionally.
        """
        from axes.middleware import AxesMiddleware

        def view(request):
            return HttpResponse("the login form", status=200)

        request = RequestFactory().post("/admin/login/", REMOTE_ADDR=CLIENT_IP)
        with override_settings(AXES_ENABLED=True):
            response = AxesMiddleware(view)(request)

        assert response.status_code == 200
