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
any kind**. That is the whole security model of this module in one observation:
the header is present exactly when Cloudflare put it there, and absent on every
path that did not come through Cloudflare.

What this gate is, and is not
-----------------------------

It is a control on the **public** path. It is not a perimeter.

The containers publish on ``0.0.0.0`` (tclancy/homelab#591), so a caller already
on the LAN reaches the admin directly and arrives with no header at all — which
under the default policy below is *allowed*. Such a caller could also simply
send ``CF-IPCountry: US`` themselves and be believed. Neither is a flaw in this
code; both are restatements of the fact that the header is only meaningful on
requests that came through Cloudflare. Treat this as defence in depth behind the
tunnel, and fix the LAN exposure where it lives, in the deployment.

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
rule and does not depend on ``ADMIN_GEO_GATE_REQUIRE_HEADER`` at all.

Refusals raise a bare ``Http404``
---------------------------------

Not 403, and not an ``HttpResponseNotFound`` built here. A 403 confirms both
that the path exists and that something is geo-gating it, which is free
reconnaissance. And raising ``Http404`` rather than returning a response lets
Django render *the application's own* 404 — so the refusal is indistinguishable
from any other missing page rather than merely uninformative.

The ``Http404`` is raised with **no message** on purpose: under ``DEBUG`` Django
renders the technical 404 page, which prints the exception's own text. A message
explaining the refusal would leak there and nowhere else, which is the worst
place for it to leak.

Wiring it up
------------

::

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "abi_django_utils.geo_gate.AdminGeoGateMiddleware",
        ...
    ]

    # Reads the ADMIN_GEO_GATE_ENABLED env var for the kill switch.
    globals().update(admin_geo_gate_defaults())

Place it early — above the session, auth and axes middleware — so a refused
request never touches the auth stack. It is cheap enough to go first: one dict
lookup and one string compare for every request that is not under a gated path.

Settings
--------

``ADMIN_GEO_GATE_ENABLED`` (default ``True``)
    The kill switch. ``False`` disables the gate entirely without a code change,
    so a trip abroad or a non-US VPN exit costs one environment variable rather
    than a deploy or a lockout.

``ADMIN_GEO_GATE_COUNTRIES`` (default ``{"US"}``)
    Allowed ISO 3166-1 alpha-2 codes. Compared upper-cased.

``ADMIN_GEO_GATE_PATHS`` (default ``("/admin/",)``)
    Path prefixes the gate applies to. Everything else — the entire public
    site — is untouched. A prefix also matches its own slashless form, so
    ``/admin/`` gates ``/admin`` too, and does not gate ``/administrator/``.

``ADMIN_GEO_GATE_REQUIRE_HEADER`` (default ``False``)
    See above. This is the load-bearing setting.
"""

from __future__ import annotations

import logging

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


def admin_geo_gate_defaults(
    *,
    countries: frozenset[str] | set[str] | None = None,
    paths: tuple[str, ...] | None = None,
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
        "ADMIN_GEO_GATE_COUNTRIES": frozenset(countries) if countries else DEFAULT_COUNTRIES,
        "ADMIN_GEO_GATE_PATHS": tuple(paths) if paths else DEFAULT_PATHS,
        "ADMIN_GEO_GATE_REQUIRE_HEADER": require_header,
    }


def path_is_gated(path: str, prefixes: tuple[str, ...]) -> bool:
    """Is ``path`` under one of ``prefixes``?

    A prefix matches its own slashless form as well, so ``/admin/`` gates both
    ``/admin/login/`` and a bare ``/admin`` — the latter matters because
    ``APPEND_SLASH`` would otherwise answer it with a redirect decided before the
    gate ever saw a country. ``/administrator/`` is not matched: the comparison is
    against the prefix, not against a bare ``/admin``.
    """
    for prefix in prefixes:
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

    Reads its settings on every request rather than caching them at
    ``__init__``, so ``override_settings`` works in tests and a settings reload
    takes effect without a restart. The cost is four ``getattr`` calls on a
    module that already did a dict lookup.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        if not getattr(settings, "ADMIN_GEO_GATE_ENABLED", True):
            return self.get_response(request)

        prefixes = getattr(settings, "ADMIN_GEO_GATE_PATHS", DEFAULT_PATHS)
        if not path_is_gated(request.path, prefixes):
            return self.get_response(request)

        country = country_of(request)
        if country is None:
            require = getattr(settings, "ADMIN_GEO_GATE_REQUIRE_HEADER", False)
            logger.warning(
                "admin geo gate: no %s on a request to %s — %s. "
                "If this came through Cloudflare, the zone's IP Geolocation setting is off.",
                COUNTRY_META_KEY.removeprefix("HTTP_").replace("_", "-"),
                request.path,
                "refusing (ADMIN_GEO_GATE_REQUIRE_HEADER)" if require else "allowing (fail open)",
            )
            if require:
                raise Http404
            return self.get_response(request)

        allowed = getattr(settings, "ADMIN_GEO_GATE_COUNTRIES", DEFAULT_COUNTRIES)
        if country not in {c.upper() for c in allowed}:
            logger.warning(
                "admin geo gate: refused %s from country %s",
                request.path,
                country,
            )
            raise Http404

        return self.get_response(request)
