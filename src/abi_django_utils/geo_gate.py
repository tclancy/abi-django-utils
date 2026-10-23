"""Refuse the admin from outside an allowed country set, using Cloudflare's ``CF-IPCountry``.

Why a header and not a GeoIP database
-------------------------------------

These apps sit behind a Cloudflare Tunnel, and Cloudflare already resolves the
caller's country at the edge and hands it to the origin as ``CF-IPCountry``
(Django exposes it as ``HTTP_CF_IPCOUNTRY``). So there is nothing to install, no
database to keep current, and no second source of truth to drift.

Measured, not assumed — and the control is the interesting half
---------------------------------------------------------------

On 2026-10-23 the header was captured on the origin side of a live request, by
sniffing the loopback socket ``cloudflared`` forwards to while a request was made
to the public hostname. Both zones:

* ``recordclub.tomclancy.info`` (zone ``tomclancy.info``) → ``Cf-Ipcountry: US``
* ``www.greaterseacoastnhindivisible.org`` → ``Cf-Ipcountry: US``

The same capture carried a control request issued *on the box* straight to
``http://localhost:8091/``, and that request arrived with **no ``Cf-*`` header of
any kind**. That is one non-Cloudflare path, measured once — enough to show the
headers are put there by Cloudflare rather than being some ambient property of
the request, and not enough to claim anything stronger. In particular a caller
on such a path can simply send ``CF-IPCountry: US`` itself and be believed.

What this gate is, and is not
-----------------------------

It is a control on the **public** path. It is not a perimeter.

The containers publish on ``0.0.0.0`` (tclancy/homelab#591), so a caller already
on the LAN reaches the admin directly. The polite version of that caller arrives
with no header and is *allowed* under the default policy below; the impolite
version supplies its own ``CF-IPCountry`` and is believed. Neither is a flaw in
this code — both are restatements of the fact that the header is only meaningful
on requests that came through Cloudflare. Treat this as defence in depth behind
the tunnel, and fix the LAN exposure where it lives, in the deployment.

The policy, and the one choice that carries all the risk
--------------------------------------------------------

``ADMIN_GEO_GATE_REQUIRE_HEADER`` decides what happens when the country is
unknown — the header is missing, empty, or Cloudflare's own "could not
geolocate" value ``XX``:

* ``False`` (the default) — **allow, and log a warning.** Right default because
  local development, LAN access, and a zone whose IP Geolocation setting is off
  all produce an absent header, and a gate that locks Tom out of his own admin
  the first time it is deployed gets removed rather than fixed. The warning is
  mandatory, not decorative: fail-open with a silent log is a gate that stops
  existing without anyone noticing.
* ``True`` — refuse. Correct once you have confirmed, the way the docstring
  above confirms it, that every path you intend to serve the admin on really
  does deliver the header.

``T1`` (Cloudflare's code for Tor) is deliberately **not** treated as unknown.
It is a real answer, it is simply not ``US``, so it is refused by the ordinary
rule and does not depend on ``ADMIN_GEO_GATE_REQUIRE_HEADER`` at all. So is any
other value Cloudflare would never send: an unrecognised country is an answer
that is not allowed, never an absence, because reading it as an absence would
turn a junk header into a way of choosing fail-open.

Refusals raise a bare ``Http404``
---------------------------------

Not 403, and not an ``HttpResponseNotFound`` built here. A 403 confirms both
that the path exists and that something is geo-gating it, which is free
reconnaissance. And raising ``Http404`` rather than returning a response lets
Django render *the application's own* 404 — so the refusal's **body** is the
body of any other missing page rather than merely uninformative.

The ``Http404`` is raised with **no message** on purpose, and this is the part
that would otherwise leak. Django's ``technical_404_response`` renders
``str(exception)`` as the page's ``reason`` whenever ``DEBUG`` is on, so a
message explaining the refusal would appear on screen in development and
nowhere else — the worst of both. The reason goes to the log instead.

**A refusal is indistinguishable from any application-raised 404, which is not
quite the same as "from a genuine 404".** Three measured differences, all
documented rather than wished away, because each is pinned by a test:

1. **``Content-Length``.** Raising from above ``CommonMiddleware`` means
   ``CommonMiddleware.process_response`` never sees the refusal, so it carries
   no ``Content-Length`` where a 404 that went through the whole stack does.
   This is the better half of a trade: mounting the gate *below*
   ``CommonMiddleware`` equalises the headers but lets ``APPEND_SLASH`` turn a
   refused ``/admin`` into a ``301`` to ``/admin/`` — which confirms the admin
   exists, i.e. exactly the reconnaissance the 404 was chosen to avoid. Mount
   it high, as below.
2. **``Resolver404`` vs ``Http404``.** A 404 template that renders
   ``{{ exception }}`` prints ``Resolver404`` for a URL that does not resolve
   and ``Http404`` for anything that raised — including this gate and including
   any view of your own. So such a template lets a refused caller tell "under
   the gated prefix" from "not a URL at all". It reveals that ``/admin/*`` is
   special; it does not reveal geo-gating. Django's built-in fallback 404 page
   renders neither.
3. **``DEBUG``.** The technical 404 page lists the URL patterns a
   resolver-raised ``Http404`` tried, and a middleware-raised one has none, so
   the two differ in development regardless of the above.

What holds in every case is the part that matters: nothing in the response says
*country*, *geo* or *Cloudflare*.

Wiring it up
------------

::

    from abi_django_utils import geo_gate

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "abi_django_utils.geo_gate.AdminGeoGateMiddleware",
        "django.middleware.common.CommonMiddleware",
        # ... session, auth, axes below
    ]

    # Reads the ADMIN_GEO_GATE_ENABLED env var for the kill switch.
    globals().update(geo_gate.admin_geo_gate_defaults())

Place it early — above ``CommonMiddleware`` for the reason above, and above the
session, auth and axes middleware so a refused request never touches the auth
stack. It is cheap enough to go first: a couple of settings reads and a prefix
compare for every request that is not under a gated path.

This is a plain sync middleware (``async_capable`` is left at Django's default
of ``False``). Under ASGI, Django will adapt it and push the rest of the chain
through a threadpool hop; every app consuming this library is WSGI today, and a
dual-mode ``__call__`` is not worth its test surface until one is not.

Settings
--------

``ADMIN_GEO_GATE_ENABLED`` (default ``True``)
    The kill switch. ``False`` disables the gate entirely without a code change,
    so a trip abroad or a non-US VPN exit costs one environment variable rather
    than a deploy or a lockout.

``ADMIN_GEO_GATE_COUNTRIES`` (default ``{"US"}``)
    Allowed ISO 3166-1 alpha-2 codes. Compared upper-cased. A bare string is
    accepted and means that one country.

``ADMIN_GEO_GATE_PATHS`` (default ``("/admin/",)``)
    Path prefixes the gate applies to, matched against ``request.path_info`` —
    so they are **URLconf-relative** and a script-prefix mount does not need
    them rewritten. Everything else — the entire public site — is untouched. A
    prefix also matches its own slashless form, so ``/admin/`` gates ``/admin``
    too, and does not gate ``/administrator/``. A bare string is accepted and
    means that one prefix.

``ADMIN_GEO_GATE_REQUIRE_HEADER`` (default ``False``)
    See above. This is the load-bearing setting.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.conf import settings
from django.http import Http404

from abi_django_utils import env

logger = logging.getLogger(__name__)

#: ``request.META`` key for Cloudflare's edge-resolved country code.
COUNTRY_META_KEY = "HTTP_CF_IPCOUNTRY"

#: Values that mean "Cloudflare did not tell us a country", as opposed to telling
#: us one we do not allow. ``XX`` is Cloudflare's own could-not-geolocate code.
#: ``T1`` (Tor) is NOT here — it is an answer, and an answer that is not allowed.
UNKNOWN_COUNTRY_CODES = frozenset({"", "XX"})

DEFAULT_COUNTRIES = frozenset({"US"})
DEFAULT_PATHS = ("/admin/",)

#: The logged country is caller-supplied and unbounded. Truncate it, so a hostile
#: caller cannot write kilobytes per request into the warning log.
MAX_LOGGED_COUNTRY_CHARS = 16


def as_prefix_tuple(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalise a path-prefix setting to a tuple.

    A bare ``str`` is the one input that must be special-cased rather than
    merely accepted, because iterating it *succeeds* and succeeds disastrously:
    ``ADMIN_GEO_GATE_PATHS = ("/admin/")`` is a missing comma away from
    ``("/admin/",)``, iterates as the characters ``/``, ``a``, ``d``…, and the
    first of those is a prefix of every path there is. The failure is a 404 on
    the whole public site, visible only in the logs.
    """
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def as_country_set(value: str | Iterable[str]) -> frozenset[str]:
    """Normalise an allowed-country setting to an upper-cased set.

    Same missing-comma hazard as :func:`as_prefix_tuple`, failing the other way:
    ``ADMIN_GEO_GATE_COUNTRIES = "US"`` iterates to ``{"U", "S"}``, which
    contains no country code at all, so every request to a gated path is
    refused and the admin is unreachable.
    """
    if isinstance(value, str):
        value = (value,)
    return frozenset(code.strip().upper() for code in value)


def admin_geo_gate_defaults(
    *,
    countries: str | Iterable[str] | None = None,
    paths: str | Iterable[str] | None = None,
    require_header: bool = False,
    env_var: str = "ADMIN_GEO_GATE_ENABLED",
) -> dict:
    """Settings for the admin geo gate, with the kill switch read from the environment.

    Shaped like :func:`abi_django_utils.tls_proxy.cloudflare_tunnel_defaults` so a
    consuming settings module can ``globals().update(...)`` it.

    The kill switch defaults to *on* when the variable is unset: an app that has
    added the middleware and forgotten the variable should be gated, not open.
    """
    return {
        "ADMIN_GEO_GATE_ENABLED": env.bool(env_var, default=True),
        "ADMIN_GEO_GATE_COUNTRIES": as_country_set(countries) if countries else DEFAULT_COUNTRIES,
        "ADMIN_GEO_GATE_PATHS": as_prefix_tuple(paths) if paths else DEFAULT_PATHS,
        "ADMIN_GEO_GATE_REQUIRE_HEADER": require_header,
    }


def gate_enabled() -> bool:
    """Is the gate switched on? Read per request so a settings reload takes effect."""
    return bool(getattr(settings, "ADMIN_GEO_GATE_ENABLED", True))


def gated_prefixes() -> tuple[str, ...]:
    """The URLconf-relative path prefixes the gate applies to."""
    return as_prefix_tuple(getattr(settings, "ADMIN_GEO_GATE_PATHS", DEFAULT_PATHS))


def allowed_countries() -> frozenset[str]:
    """The upper-cased set of country codes allowed through a gated path."""
    return as_country_set(getattr(settings, "ADMIN_GEO_GATE_COUNTRIES", DEFAULT_COUNTRIES))


def header_required() -> bool:
    """Refuse, rather than allow, when the country is unknown?"""
    return bool(getattr(settings, "ADMIN_GEO_GATE_REQUIRE_HEADER", False))


def path_is_gated(path: str, prefixes: str | Iterable[str]) -> bool:
    """Is ``path`` under one of ``prefixes``?

    A prefix matches its own slashless form as well, so ``/admin/`` gates both
    ``/admin/login/`` and a bare ``/admin``. ``/administrator/`` is not matched:
    the comparison is against the prefix, not against a bare ``/admin``.
    """
    for prefix in as_prefix_tuple(prefixes):
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            return True
    return False


def country_of(request) -> str | None:
    """The caller's country per Cloudflare, or ``None`` if it did not say.

    ``None`` covers three cases a caller cannot tell apart and neither can we:
    the header was absent, it was empty, or it was ``XX``.
    """
    raw = request.META.get(COUNTRY_META_KEY)
    if raw is None:
        return None
    code = raw.strip().upper()
    if code in UNKNOWN_COUNTRY_CODES:
        return None
    return code


class AdminGeoGateMiddleware:
    """Refuse gated paths from outside ``ADMIN_GEO_GATE_COUNTRIES``.

    Matches on ``request.path_info`` rather than ``request.path``, so the
    configured prefixes are the ones in the app's ``urls.py``. ``request.path``
    is ``SCRIPT_NAME + path_info``, so under ``FORCE_SCRIPT_NAME`` or a WSGI
    mount a prefix of ``/admin/`` would stop matching ``/<prefix>/admin/`` and
    the gate would quietly stop gating — with no warning, because the refusal
    check would never be reached.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not gate_enabled():
            return self.get_response(request)

        path = request.path_info
        if not path_is_gated(path, gated_prefixes()):
            return self.get_response(request)

        country = country_of(request)
        if country is None:
            return self._unknown_country(request, path)

        if country not in allowed_countries():
            logger.warning(
                "admin geo gate: refused %s from country %s",
                path,
                country[:MAX_LOGGED_COUNTRY_CHARS],
            )
            raise Http404

        return self.get_response(request)

    def _unknown_country(self, request, path):
        """Cloudflare did not say where this came from. Warn, then apply the policy."""
        require = header_required()
        logger.warning(
            "admin geo gate: no %s on a request to %s — %s. "
            "If this came through Cloudflare, the zone's IP Geolocation setting is off.",
            COUNTRY_META_KEY.removeprefix("HTTP_").replace("_", "-"),
            path,
            "refusing (ADMIN_GEO_GATE_REQUIRE_HEADER)" if require else "allowing (fail open)",
        )
        if require:
            raise Http404
        return self.get_response(request)
