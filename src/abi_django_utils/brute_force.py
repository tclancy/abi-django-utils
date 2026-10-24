"""django-axes defaults for apps whose login page faces the internet.

Of the five public Django apps this library serves, two ran django-axes and
disagreed on the one setting that decides what a lockout is keyed on:
``heydover`` pinned ``AXES_IPWARE_META_PRECEDENCE_ORDER`` and ``indivisible``,
axes-wired since 2026-04-13, did not. This module holds the agreement in one
place so an app writes a handful of assignments instead of a copy of the block
(tclancy/abi-django-utils#16; the exposure survey behind that ticket is its
own, not re-measured here).

Every setting here is pinned, including the ones whose value matches
django-axes's own default. An unpinned key is not an agreement -- it is an
inheritance that a release can change under all five apps at once, and that no
app's settings module mentions. ``AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT``
was the last of those (abi-django-utils#22) and is now
:func:`axes_defaults`'s ``cool_off_resets_on_failure``.

Install with the extra, not bare::

    abi-django-utils[axes]

``django-axes`` declares ``django-ipware`` under its own ``ipware`` extra and
resolves the client IP through it. Without ipware on the path,
``AXES_IPWARE_META_PRECEDENCE_ORDER`` is a list django-axes never reads, and
``get_client_ip_address`` returns ``request.META["REMOTE_ADDR"]`` -- behind a
Cloudflare tunnel, the gateway. :func:`axes_defaults` refuses to emit the
setting in that state rather than hand back config that reads as protection.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence

from django.core.exceptions import ImproperlyConfigured

#: ``request.META`` keys django-ipware walks, in order, to find the client.
#: Cloudflare sets and overwrites ``CF-Connecting-IP`` on every request through
#: the tunnel, so for traffic that transits Cloudflare it is the only one of the
#: three a client cannot choose. ``X-Forwarded-For`` is the fallback for another
#: proxy and ``REMOTE_ADDR`` for a direct request.
#:
#: **This order assumes Cloudflare is the only route to the app.** A request
#: that reaches Django without transiting the tunnel can set either header
#: itself, and gets a lockout key of its choosing -- a fresh one per attempt.
#: Measured against ``axes.helpers.get_client_ip_address`` with this order
#: active: a direct request claiming ``CF-Connecting-IP: 1.2.3.4`` resolves to
#: ``1.2.3.4``. Every homelab Django compose template publishes its port
#: without a ``127.0.0.1:`` prefix, so that route is open on the LAN today;
#: closing it belongs in the deployment, not here. Closing it *here* would mean
#: ``AXES_CLIENT_IP_CALLABLE`` and a per-deployment list of trusted proxy
#: addresses -- see abi-django-utils#16.
CLOUDFLARE_META_PRECEDENCE_ORDER = (
    "HTTP_CF_CONNECTING_IP",
    "HTTP_X_FORWARDED_FOR",
    "REMOTE_ADDR",
)

#: Monitors authentication without also *performing* it, so it has to sit ahead
#: of the backend that does.
AXES_BACKEND = "axes.backends.AxesStandaloneBackend"
DJANGO_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
AXES_APP = "axes"
AXES_MIDDLEWARE = "axes.middleware.AxesMiddleware"


def ipware_installed() -> bool:
    """Report whether django-ipware is importable.

    This answers the same question ``axes.helpers.IPWARE_INSTALLED`` answers --
    it is computed there from ``import ipware.ip`` -- by looking the module up
    instead of importing it. The caller is a Django settings module and a
    look-up has no import side effects at all, where ``import ipware.ip`` would
    execute ``ipware/__init__.py`` and pull in ``django.http`` for a question
    that does not need either. (That import does *work* mid-settings; it is
    simply more than the probe needs.)

    One state the two can disagree in: django-ipware 8 requires the separate
    ``python-ipware`` distribution, so a half-installed ipware answers yes here
    and no in axes, and :func:`axes_defaults` would then emit the dead setting
    it exists to refuse. ``test_agrees_with_axes_own_probe`` catches that only
    in an environment already in that state.
    """
    return importlib.util.find_spec("ipware") is not None


def with_axes_backend(
    backends: Sequence[str] = (DJANGO_MODEL_BACKEND,),
) -> list[str]:
    """Return ``backends`` with the axes backend first, and nothing reordered.

    Position is the whole content of this function: the standalone backend
    records the attempt and raises on a locked-out client, and a backend that
    authenticates first would have already answered.
    """
    return [AXES_BACKEND, *(b for b in backends if b != AXES_BACKEND)]


def with_axes_app(installed_apps: Sequence[str]) -> list[str]:
    """Return ``installed_apps`` with the axes app appended, idempotently."""
    return [*installed_apps, *([] if AXES_APP in installed_apps else [AXES_APP])]


def with_axes_middleware(middleware: Sequence[str]) -> list[str]:
    """Return ``middleware`` with the axes middleware last, moving it if needed.

    It turns the backend's lockout flag into the refusal the client sees --
    ``AXES_HTTP_RESPONSE_CODE``, which django-axes 8.3.1 defaults to **429**,
    not 403.

    **Presence is the property that matters, not position.**
    ``AxesMiddleware.__call__`` awaits ``get_response(request)`` before it
    consults ``request.axes_locked_out``, so it is response-phase only and
    wraps the view from any index. Measured independently twice --
    ``tclancy/itemshop#372`` moved the entry to the front of ``MIDDLEWARE`` and
    the lockout still answered 429, and
    ``test_the_flag_is_read_after_the_view_so_position_cannot_matter`` drives
    the real middleware here. An earlier version of this docstring argued the
    opposite ("it has to wrap the view rather than sit in front of it"), which
    would have read as a constraint on an app that had it mid-list.

    Last is kept as a convention, for two reasons that are both weaker than a
    constraint: it is what django-axes documents, and it is where both apps
    that already ran axes put it. An entry found elsewhere is moved so the
    convention holds, not because leaving it would break the lockout.
    """
    return [*(m for m in middleware if m != AXES_MIDDLEWARE), AXES_MIDDLEWARE]


def axes_defaults(
    *,
    only_admin_site: bool = True,
    failure_limit: int = 5,
    cooloff_hours: int = 1,
    cool_off_resets_on_failure: bool = True,
    backends: Sequence[str] = (DJANGO_MODEL_BACKEND,),
    proxy_aware: bool = True,
) -> dict[str, object]:
    """Return django-axes settings for a public login page behind a TLS proxy.

    Values are ``heydover``'s, which has run them in production longest.

    ``only_admin_site`` defaults to ``True``, and all three consumers named on
    the ticket take that default: none of them routes a non-admin login through
    ``django.contrib.auth.authenticate()``, which is the only kind of login
    django-axes can observe, so ``False`` would widen nothing while giving up
    the narrowing on ``/admin/``. Pass ``False`` for an app that does have such
    a login. Note what this implies: a login that bypasses
    ``authenticate()`` -- ``recordclub``'s magic-link POST at ``/auth/login/``
    is one -- is invisible to django-axes at any setting, and needs a different
    control.

    ``cooloff_hours`` is an hour of **silence**, not an hour from the failure
    that crossed the limit -- see ``cool_off_resets_on_failure`` below, which is
    what makes that true and is the reason this paragraph exists.

    ``proxy_aware=False`` omits ``AXES_IPWARE_META_PRECEDENCE_ORDER`` entirely
    and accepts that the lockout keys on the proxy's address, shared by every
    client. It exists so that state has to be asked for.

    ``cool_off_resets_on_failure`` emits
    ``AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT``, which django-axes 8.3.1
    defaults to ``True`` and which this helper previously left unpinned -- a
    sixth key every consumer inherited from upstream rather than agreed on
    here, which is the drift abi-django-utils#16 existed to stop.

    ``True`` (the default, and what every app runs today) means a request made
    while already locked out is itself recorded as a further failure: it moves
    ``attempt_time`` and restarts the clock. ``False`` means a fixed window --
    axes's ``AxesStandaloneBackend`` sets ``request.axes_locked_out`` and the
    database handler returns before the ``get_or_create``, so the refused
    request is not recorded at all.

    The trade, which is abi-django-utils#22 and is **Tom's to settle**, not
    this helper's:

    * ``True`` is strictly better against a bot that hammers -- its own
      persistence keeps it locked out -- and equal against one that backs off
      exactly one cooloff. The guess *rate* is ``failure_limit`` per cooloff
      either way.
    * ``True`` is worse for the sole admin of each of these apps, because
      retrying is the obvious human response to a refusal and it extends the
      lockout. Recovery is ``manage.py axes_reset_ip <address>`` on the box.
    * ``True`` also keeps the row unreapable: ``clean_expired_user_attempts``
      deletes by ``attempt_time__lte``, and the handler ``Concat``-appends to
      ``post_data`` on every failure, so a persistent attacker grows one
      sqlite ``TEXT`` column without bound.

    The default is ``True`` because that is behaviour-preserving: no app pins
    this key, so all of them run ``True`` today, and ``recordclub`` (the only
    app consuming this helper) left it at the upstream default deliberately and
    pinned it with a behavioural test. Pinning it here changes nothing a
    consumer observes; it makes the value an agreement instead of an
    inheritance, and gives an app that wants a fixed window one keyword instead
    of a seventh inlined ``AXES_`` assignment.

    :raises ImproperlyConfigured: if ``proxy_aware`` is set and django-ipware
        is not installed, in which case django-axes would ignore the precedence
        order and resolve every tunnelled request to the gateway.
    """
    settings: dict[str, object] = {
        "AUTHENTICATION_BACKENDS": with_axes_backend(backends),
        "AXES_FAILURE_LIMIT": failure_limit,
        "AXES_COOLOFF_TIME": cooloff_hours,
        "AXES_LOCKOUT_PARAMETERS": [["ip_address"]],
        "AXES_RESET_ON_SUCCESS": True,
        "AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT": cool_off_resets_on_failure,
        "AXES_ONLY_ADMIN_SITE": only_admin_site,
    }

    if not proxy_aware:
        return settings

    if not ipware_installed():
        raise ImproperlyConfigured(
            "AXES_IPWARE_META_PRECEDENCE_ORDER needs django-ipware, which "
            "django-axes declares under its own extra. Depend on "
            "django-axes[ipware] (or abi-django-utils[axes]), or pass "
            "proxy_aware=False to accept a lockout keyed on the proxy address "
            "shared by every client."
        )

    settings["AXES_IPWARE_META_PRECEDENCE_ORDER"] = list(CLOUDFLARE_META_PRECEDENCE_ORDER)
    return settings
