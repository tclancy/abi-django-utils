"""django-axes defaults for apps whose login page faces the internet.

Of the five public Django apps this library serves, two ran django-axes and
disagreed on the one setting that decides what a lockout is keyed on:
``heydover`` pinned ``AXES_IPWARE_META_PRECEDENCE_ORDER`` and ``indivisible``,
axes-wired since 2026-04-13, did not. This module holds the agreement in one
place so an app writes a handful of assignments instead of a copy of the block
(tclancy/abi-django-utils#16; the exposure survey behind that ticket is its
own, not re-measured here).

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
#: the tunnel, so it is the only one of the three a client cannot choose.
#: ``X-Forwarded-For`` is kept as the fallback for a non-Cloudflare proxy and
#: ``REMOTE_ADDR`` for a direct request.
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
    it is computed there from ``import ipware.ip`` -- but by looking the module
    up rather than importing it. The caller is a Django settings module:
    ``ipware.ip`` imports ``django.http`` at module level, and pulling that in
    while ``django.conf.settings`` is still being assembled is a worse failure
    than the one being prevented. ``find_spec`` touches neither.

    ``test_agrees_with_axes_own_probe`` is what keeps the two in step.
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

    It turns a lockout signal into the 403 the client sees, which means it has
    to wrap the view rather than sit in front of it; last in the base list is
    where both apps here that already run django-axes put it. An entry found
    elsewhere in the list is moved rather than left, because being present is
    not the property that matters.
    """
    return [*(m for m in middleware if m != AXES_MIDDLEWARE), AXES_MIDDLEWARE]


def axes_defaults(
    *,
    only_admin_site: bool = True,
    failure_limit: int = 5,
    cooloff_hours: int = 1,
    backends: Sequence[str] = (DJANGO_MODEL_BACKEND,),
    proxy_aware: bool = True,
) -> dict:
    """Return django-axes settings for a public login page behind a TLS proxy.

    Values are ``heydover``'s, which has run them in production longest.

    ``only_admin_site`` defaults to ``True`` because the exposure this answers
    is an admin-login enumeration and ``True`` is the narrowest change for an
    app adopting axes from nothing -- it cannot lock a legitimate user out of a
    surface it does not watch. Pass ``False`` for an app with its own public
    login (``recordclub`` serves one at ``/auth/login/``) or for one already
    running axes without the flag, where ``True`` would *narrow* what is
    covered today.

    ``proxy_aware=False`` omits ``AXES_IPWARE_META_PRECEDENCE_ORDER`` entirely
    and accepts that the lockout keys on the proxy's address, shared by every
    client. It exists so that state has to be asked for.

    :raises ImproperlyConfigured: if ``proxy_aware`` is set and django-ipware
        is not installed, in which case django-axes would ignore the precedence
        order and resolve every tunnelled request to the gateway.
    """
    settings: dict = {
        "AUTHENTICATION_BACKENDS": with_axes_backend(backends),
        "AXES_FAILURE_LIMIT": failure_limit,
        "AXES_COOLOFF_TIME": cooloff_hours,
        "AXES_LOCKOUT_PARAMETERS": [["ip_address"]],
        "AXES_RESET_ON_SUCCESS": True,
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
