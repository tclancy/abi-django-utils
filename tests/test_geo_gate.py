"""Tests for the Cloudflare ``CF-IPCountry`` admin geo gate."""

from __future__ import annotations

import logging

import pytest
from django.http import Http404
from django.test import Client
from django.test.utils import override_settings

from abi_django_utils import geo_gate
from abi_django_utils.geo_gate import AdminGeoGateMiddleware, admin_geo_gate_defaults, country_of, path_is_gated

GATED = dict(
    ROOT_URLCONF="tests.geo_gate_urls",
    MIDDLEWARE=["abi_django_utils.geo_gate.AdminGeoGateMiddleware"],
    ADMIN_GEO_GATE_ENABLED=True,
    ADMIN_GEO_GATE_COUNTRIES=frozenset({"US"}),
    ADMIN_GEO_GATE_PATHS=("/admin/",),
    ADMIN_GEO_GATE_REQUIRE_HEADER=False,
)

# Phrases a refusal must never put in front of the caller. "geo"/"country"/
# "cloudflare" would say a gate exists; the code itself would say which country
# was read; "admin geo gate" is the log line's own prefix, which must stay in the
# log and out of the response.
LEAK_PHRASES = ("geo", "country", "cloudflare", "cf-ipcountry", "admin geo gate", "refus", "forbidden")


def scan_for_leaks(text: str) -> list[str]:
    """Which :data:`LEAK_PHRASES` appear in ``text``? Case-insensitively."""
    low = text.lower()
    return [phrase for phrase in LEAK_PHRASES if phrase in low]


def get(path, country=None, **extra):
    """GET ``path`` through the test client, optionally carrying a country header."""
    if country is not None:
        extra["HTTP_CF_IPCOUNTRY"] = country
    return Client().get(path, **extra)


# --------------------------------------------------------------------------
# The success criteria from the ticket, one test each.
# --------------------------------------------------------------------------


@override_settings(**GATED)
def test_an_allowed_country_reaches_the_admin_view():
    response = get("/admin/login/", country="US")
    assert response.status_code == 200
    assert response.content == b"ADMIN-LOGIN-BODY"


@override_settings(**GATED)
def test_a_disallowed_country_is_refused():
    response = get("/admin/login/", country="GB")
    assert response.status_code == 404
    assert b"ADMIN-LOGIN-BODY" not in response.content


@override_settings(**GATED)
@pytest.mark.parametrize("country", ["US", "GB"])
def test_the_public_site_is_untouched_by_either_country(country):
    response = get("/", country=country)
    assert response.status_code == 200
    assert response.content == b"PUBLIC-SITE-BODY"


@override_settings(**GATED)
def test_a_refusals_body_is_that_of_a_genuine_404():
    """The whole point of raising ``Http404`` instead of returning a 403.

    One request is refused for its country, the other genuinely does not
    resolve, and the bodies must match.

    Note what this does *not* establish, because an earlier version of this test
    claimed it did: Django's fallback 404 page renders a fixed title and
    sentence and does **not** echo the request path, so two 404s on different
    paths are byte-identical too. The equality here is therefore a weak check —
    :func:`test_a_refusal_does_not_leak_through_a_template_that_renders_the_exception`
    is the one with teeth, and it finds a difference this one cannot see.
    """
    refused = get("/admin/ghost/", country="GB")
    genuine = get("/admin/ghost/", country="US")

    assert refused.status_code == genuine.status_code == 404
    assert refused.content == genuine.content


# A 404 template that interpolates the exception, which is what Django's own
# technical 404 page does under DEBUG. Without something like this in the stack
# an "it does not leak" assertion over the body cannot fail, because the
# fallback error page interpolates nothing at all.
EXCEPTION_RENDERING_404 = {
    "TEMPLATES": [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "OPTIONS": {
                "loaders": [
                    (
                        "django.template.loaders.locmem.Loader",
                        {"404.html": "NOT-FOUND[{{ exception }}]"},
                    )
                ],
            },
        }
    ]
}


@override_settings(**{**GATED, **EXCEPTION_RENDERING_404})
def test_a_refusal_does_not_leak_through_a_template_that_renders_the_exception():
    """With the exception on screen, the refusal still says what a genuine miss says.

    Django's ``page_not_found`` puts ``exception.args[0]`` in the context when
    there is one and the class name ``Http404`` when there is not — so an
    argument-free raise renders exactly what a view's own argument-free raise
    renders, and the two are compared rather than guessed at.
    """
    refused = get("/admin/login/", country="GB")
    app_raised = get("/admin/bare/", country="US")
    unresolved = get("/admin/ghost/", country="US")

    assert refused.status_code == app_raised.status_code == unresolved.status_code == 404

    body = refused.content.decode()
    assert scan_for_leaks(body) == []
    assert "GB" not in body

    # The equivalence that holds: a refusal is any application-raised 404.
    assert body == app_raised.content.decode() == "NOT-FOUND[Http404]"

    # The one that does not, and is documented rather than wished away: a URL
    # that does not resolve raises Resolver404, and this template says so. So
    # under a 404 template of this shape a refused caller can tell "under the
    # gated prefix" from "not a URL at all". It reveals that /admin/* is
    # special; it does not reveal geo-gating, and the project 404 templates in
    # play render neither.
    assert unresolved.content.decode() == "NOT-FOUND[Resolver404]"


@override_settings(**{**GATED, **EXCEPTION_RENDERING_404})
def test_the_404_template_really_does_surface_an_http404_message():
    """Reachability control for the test above, and the only thing that makes it real.

    ``/admin/leaky/`` raises ``Http404("LEAKY-SENTINEL-abc123")`` from a view. If
    that string does not reach the body, then the absence assertion above is
    passing because nothing could ever have appeared there.
    """
    leaked = get("/admin/leaky/", country="US")

    assert leaked.status_code == 404
    assert "LEAKY-SENTINEL-abc123" in leaked.content.decode()


@override_settings(**GATED)
def test_a_refusal_leaks_nothing_through_its_headers():
    response = get("/admin/login/", country="GB")
    assert response.status_code == 404
    assert scan_for_leaks("\n".join(f"{k}: {v}" for k, v in response.items())) == []


def test_the_leak_scanner_can_actually_find_a_leak():
    """Reachability control for every ``scan_for_leaks(...) == []`` above.

    That assertion passes just as happily against a scanner that can never match
    anything. Drive it to a finding on a synthetic response.
    """
    leaking = "403 Forbidden: your Country is not allowed (geo gate, Cloudflare CF-IPCountry)"
    found = set(scan_for_leaks(leaking))
    assert found == {"geo", "country", "cloudflare", "cf-ipcountry", "forbidden"}, found


# --------------------------------------------------------------------------
# A script-prefix mount, and the two one-character config typos.
#
# All three are silent: the gate either stops gating or gates everything, and
# nothing in the response says which. They were found in review, not by the
# mutation round, because `request.path` and `request.path_info` are identical
# in every test that does not set SCRIPT_NAME.
# --------------------------------------------------------------------------


@override_settings(**{**GATED, "FORCE_SCRIPT_NAME": "/recordclub"})
def test_a_script_prefix_mount_is_still_gated():
    """``request.path`` would be ``/recordclub/admin/login/`` and match nothing."""
    assert get("/admin/login/", country="GB").status_code == 404
    assert get("/admin/login/", country="US").status_code == 200


@override_settings(**{**GATED, "FORCE_SCRIPT_NAME": "/recordclub"})
def test_a_script_prefix_mount_does_not_gate_the_public_site():
    response = get("/", country="GB")
    assert response.status_code == 200
    assert response.content == b"PUBLIC-SITE-BODY"


@override_settings(**{**GATED, "ADMIN_GEO_GATE_PATHS": "/admin/"})
def test_a_bare_string_path_setting_gates_only_the_admin():
    """The missing comma. Iterating the string yields ``/``, a prefix of everything."""
    assert get("/admin/login/", country="GB").status_code == 404
    assert get("/", country="GB").status_code == 200
    assert get("/administrator/", country="GB").status_code == 200


@override_settings(**{**GATED, "ADMIN_GEO_GATE_COUNTRIES": "US"})
def test_a_bare_string_country_setting_means_that_one_country():
    """The same typo failing the other way: ``{"U", "S"}`` allows nobody."""
    assert get("/admin/login/", country="US").status_code == 200
    assert get("/admin/login/", country="GB").status_code == 404


# --------------------------------------------------------------------------
# No ADMIN_GEO_GATE_* settings at all.
#
# Every test above sets all four through override_settings, which leaves the
# middleware's own ``getattr`` fallbacks unexecuted -- a mutation round found
# ``ADMIN_GEO_GATE_ENABLED`` defaulting to False surviving all 54 of them. The
# test settings module defines none of these keys, so overriding only the two
# Django ones is what drives the real defaults.
# --------------------------------------------------------------------------

BARE = dict(ROOT_URLCONF="tests.geo_gate_urls", MIDDLEWARE=["abi_django_utils.geo_gate.AdminGeoGateMiddleware"])


@override_settings(**BARE)
def test_with_no_gate_settings_at_all_the_gate_is_on():
    """An app that adds the middleware and forgets the settings must be gated, not open."""
    assert get("/admin/login/", country="GB").status_code == 404
    assert get("/admin/login/", country="US").status_code == 200


@override_settings(**BARE)
def test_with_no_gate_settings_the_default_prefix_is_the_admin_only():
    assert get("/", country="GB").status_code == 200
    assert get("/administrator/", country="GB").status_code == 200


@override_settings(**BARE)
def test_with_no_gate_settings_an_unknown_country_still_fails_open(caplog):
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/")
    assert response.status_code == 200
    assert "allowing (fail open)" in caplog.text


# --------------------------------------------------------------------------
# The unknown-country policy: the load-bearing choice.
# --------------------------------------------------------------------------


@override_settings(**GATED)
@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("XX", id="cloudflare-could-not-geolocate"),
    ],
)
def test_an_unknown_country_fails_open_and_warns(header, caplog):
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/", country=header)

    assert response.status_code == 200
    assert response.content == b"ADMIN-LOGIN-BODY"
    assert "allowing (fail open)" in caplog.text
    assert "CF-IPCOUNTRY" in caplog.text


@override_settings(**{**GATED, "ADMIN_GEO_GATE_REQUIRE_HEADER": True})
@pytest.mark.parametrize("header", [None, "", "XX"])
def test_require_header_refuses_an_unknown_country_and_still_warns(header, caplog):
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/", country=header)

    assert response.status_code == 404
    assert "refusing (ADMIN_GEO_GATE_REQUIRE_HEADER)" in caplog.text


@override_settings(**GATED)
def test_a_junk_country_header_cannot_flood_the_log(caplog):
    """The logged value is caller-supplied and unbounded, so it is truncated.

    Any value Cloudflare would never send is still an *answer* that is not
    allowed, never an absence — reading junk as an absence would make a junk
    header a way of choosing fail-open. So it is refused, and the only question
    is how much of it reaches the log.
    """
    junk = "Z" * 5000
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/", country=junk)

    assert response.status_code == 404
    # django.request logs its own "Not Found" line; only ours is under test.
    (record,) = [r for r in caplog.records if r.name == "abi_django_utils.geo_gate"]
    assert record.args[-1] == "Z" * geo_gate.MAX_LOGGED_COUNTRY_CHARS
    assert len(record.getMessage()) < 200, "the whole header must not reach the log"


@override_settings(**GATED)
def test_an_allowed_country_logs_no_warning(caplog):
    """Control for every ``in caplog.text`` assertion above.

    If the gate warned on the happy path too, "a warning was emitted" would be
    evidence of nothing.
    """
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/", country="US")

    assert response.status_code == 200
    assert caplog.records == []


@override_settings(**GATED)
def test_tor_is_refused_without_depending_on_the_require_header_setting(caplog):
    """``T1`` is an answer, not an absence — so the unknown policy must not reach it."""
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/", country="T1")

    assert response.status_code == 404
    assert "from country T1" in caplog.text
    assert "fail open" not in caplog.text


# --------------------------------------------------------------------------
# The kill switch, in both positions.
# --------------------------------------------------------------------------


@override_settings(**{**GATED, "ADMIN_GEO_GATE_ENABLED": False})
def test_the_kill_switch_off_lets_a_refused_country_through(caplog):
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        response = get("/admin/login/", country="GB")

    assert response.status_code == 200
    assert response.content == b"ADMIN-LOGIN-BODY"
    assert caplog.records == [], "a disabled gate should be silent, not merely permissive"


@override_settings(**{**GATED, "ADMIN_GEO_GATE_ENABLED": True})
def test_the_kill_switch_on_refuses_the_same_request():
    assert get("/admin/login/", country="GB").status_code == 404


@override_settings(**{**GATED, "ADMIN_GEO_GATE_ENABLED": False})
def test_the_kill_switch_off_does_not_even_warn_about_a_missing_header(caplog):
    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        assert get("/admin/login/").status_code == 200
    assert caplog.records == []


# --------------------------------------------------------------------------
# Normalisation and path matching.
# --------------------------------------------------------------------------


@override_settings(**GATED)
@pytest.mark.parametrize("header", ["us", " US ", "Us", "\tus\n"])
def test_the_country_code_is_compared_case_and_whitespace_insensitively(header):
    assert get("/admin/login/", country=header).status_code == 200


@override_settings(**GATED)
@pytest.mark.parametrize("header", ["gb", " GB "])
def test_normalisation_does_not_accidentally_allow_a_refused_country(header):
    assert get("/admin/login/", country=header).status_code == 404


@override_settings(**{**GATED, "ADMIN_GEO_GATE_COUNTRIES": frozenset({"us", "ca"})})
def test_a_lowercase_allowed_set_in_settings_still_matches():
    assert get("/admin/login/", country="CA").status_code == 200
    assert get("/admin/login/", country="MX").status_code == 404


@override_settings(**GATED)
def test_the_slashless_form_of_the_prefix_is_gated():
    assert get("/admin", country="US").status_code == 200
    assert get("/admin", country="GB").status_code == 404


@override_settings(**GATED)
@pytest.mark.parametrize("country", ["US", "GB"])
def test_a_path_that_merely_starts_with_the_prefixs_letters_is_not_gated(country):
    response = get("/administrator/", country=country)
    assert response.status_code == 200
    assert response.content == b"ADMINISTRATOR-BODY"


@override_settings(**{**GATED, "ADMIN_GEO_GATE_PATHS": ("/admin/", "/administrator/")})
def test_more_than_one_prefix_is_honoured():
    """One prefix in the tuple is indistinguishable from a loop that returns early."""
    assert get("/admin/login/", country="GB").status_code == 404
    assert get("/administrator/", country="GB").status_code == 404
    assert get("/", country="GB").status_code == 200


# --------------------------------------------------------------------------
# Unit-level: the two helpers and the settings factory.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/admin/", True),
        ("/admin", True),
        ("/admin/login/", True),
        ("/administrator/", False),
        ("/adminx", False),
        ("/", False),
        ("/x/admin/", False),
    ],
)
def test_path_is_gated(path, expected):
    assert path_is_gated(path, ("/admin/",)) is expected


class FakeRequest:
    def __init__(self, **meta):
        self.META = meta
        self.path_info = "/admin/login/"
        # SCRIPT_NAME-prefixed, deliberately different from path_info: a
        # middleware that reads `path` instead would not match the prefix.
        self.path = "/mounted/admin/login/"


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({}, None),
        ({"HTTP_CF_IPCOUNTRY": ""}, None),
        ({"HTTP_CF_IPCOUNTRY": "  "}, None),
        ({"HTTP_CF_IPCOUNTRY": "XX"}, None),
        ({"HTTP_CF_IPCOUNTRY": "xx"}, None),
        ({"HTTP_CF_IPCOUNTRY": "US"}, "US"),
        ({"HTTP_CF_IPCOUNTRY": "us"}, "US"),
        ({"HTTP_CF_IPCOUNTRY": "T1"}, "T1"),
    ],
)
def test_country_of(meta, expected):
    assert country_of(FakeRequest(**meta)) == expected


def test_tor_is_not_in_the_unknown_set():
    """Pins the decision, not just the behaviour it currently produces."""
    assert "T1" not in geo_gate.UNKNOWN_COUNTRY_CODES
    assert "XX" in geo_gate.UNKNOWN_COUNTRY_CODES


def test_defaults_are_on_when_the_env_var_is_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_GEO_GATE_ENABLED", raising=False)
    settings = admin_geo_gate_defaults()
    assert settings["ADMIN_GEO_GATE_ENABLED"] is True
    assert settings["ADMIN_GEO_GATE_COUNTRIES"] == frozenset({"US"})
    assert settings["ADMIN_GEO_GATE_PATHS"] == ("/admin/",)
    assert settings["ADMIN_GEO_GATE_REQUIRE_HEADER"] is False


@pytest.mark.parametrize("raw,expected", [("0", False), ("false", False), ("no", False), ("1", True), ("on", True)])
def test_the_env_kill_switch_is_read(monkeypatch, raw, expected):
    monkeypatch.setenv("ADMIN_GEO_GATE_ENABLED", raw)
    assert admin_geo_gate_defaults()["ADMIN_GEO_GATE_ENABLED"] is expected


def test_the_factory_overrides_are_applied(monkeypatch):
    monkeypatch.delenv("ADMIN_GEO_GATE_ENABLED", raising=False)
    settings = admin_geo_gate_defaults(countries={"ca"}, paths=("/backoffice/",), require_header=True)
    assert settings["ADMIN_GEO_GATE_COUNTRIES"] == frozenset({"CA"}), "the factory normalises case"
    assert settings["ADMIN_GEO_GATE_PATHS"] == ("/backoffice/",)
    assert settings["ADMIN_GEO_GATE_REQUIRE_HEADER"] is True


def test_the_env_var_name_is_configurable(monkeypatch):
    monkeypatch.setenv("RECORDCLUB_GEO_GATE", "0")
    monkeypatch.delenv("ADMIN_GEO_GATE_ENABLED", raising=False)
    assert admin_geo_gate_defaults(env_var="RECORDCLUB_GEO_GATE")["ADMIN_GEO_GATE_ENABLED"] is False


@override_settings(**GATED)
def test_the_middleware_raises_a_message_free_http404(caplog):
    """Under ``DEBUG`` Django prints the exception's own text on the 404 page.

    A message explaining the refusal would leak there and nowhere else, so the
    ``Http404`` must carry no arguments at all.
    """
    middleware = AdminGeoGateMiddleware(lambda request: pytest.fail("get_response must not be called"))
    request = FakeRequest(HTTP_CF_IPCOUNTRY="GB")

    with caplog.at_level(logging.WARNING, logger="abi_django_utils.geo_gate"):
        with pytest.raises(Http404) as raised:
            middleware(request)

    assert raised.value.args == ()
    assert str(raised.value) == ""
    # The reason lives in the log instead, which is the point.
    assert "from country GB" in caplog.text


# --------------------------------------------------------------------------
# A realistic middleware stack.
#
# Every test above runs with MIDDLEWARE = [the gate], which is the one
# configuration in which "the refusal is indistinguishable from a genuine 404"
# is unconditionally true. With CommonMiddleware in the stack it is true of the
# body and false of the framing, and the direction of that difference depends on
# the ordering -- so the ordering the README recommends is worth pinning.
# --------------------------------------------------------------------------

GATE_ABOVE_COMMON = [
    "django.middleware.security.SecurityMiddleware",
    "abi_django_utils.geo_gate.AdminGeoGateMiddleware",
    "django.middleware.common.CommonMiddleware",
]

GATE_BELOW_COMMON = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "abi_django_utils.geo_gate.AdminGeoGateMiddleware",
]


@override_settings(**{**GATED, "MIDDLEWARE": GATE_ABOVE_COMMON})
def test_in_the_recommended_ordering_the_body_matches_a_genuine_404():
    refused = get("/admin/login/", country="GB")
    genuine = get("/admin/ghost/", country="US")

    assert refused.status_code == genuine.status_code == 404
    assert refused.content == genuine.content


@override_settings(**{**GATED, "MIDDLEWARE": GATE_ABOVE_COMMON})
def test_in_the_recommended_ordering_the_refusal_has_no_content_length():
    """Pins the known, accepted distinguisher rather than claiming it away.

    ``CommonMiddleware.process_response`` sets ``Content-Length`` and never sees
    a response short-circuited above it. Documented in the module docstring; if
    this test ever fails, that paragraph is what needs rewriting.
    """
    refused = get("/admin/login/", country="GB")
    genuine = get("/admin/ghost/", country="US")

    assert refused.get("Content-Length") is None
    assert genuine.get("Content-Length") is not None


@override_settings(**{**GATED, "MIDDLEWARE": GATE_ABOVE_COMMON, "APPEND_SLASH": True})
def test_the_recommended_ordering_does_not_redirect_a_refused_slashless_path():
    """The reason the gate goes *above* ``CommonMiddleware``.

    A 301 to ``/admin/`` would confirm the admin exists, which is the
    reconnaissance the 404 was chosen to avoid. ``APPEND_SLASH`` runs in
    ``CommonMiddleware.process_response``, so it only sees the refusal if the
    gate is below it.
    """
    assert get("/admin", country="GB").status_code == 404


@override_settings(**{**GATED, "MIDDLEWARE": GATE_BELOW_COMMON, "APPEND_SLASH": True})
def test_the_other_ordering_is_still_a_refusal_not_a_redirect_here():
    """Control for the test above: the 404 is not an artefact of the ordering alone.

    ``/admin`` resolves in this urlconf, so ``APPEND_SLASH`` has nothing to
    redirect to in either ordering, and both read 404. That is exactly why the
    docstring's claim about the other ordering is argued from
    ``CommonMiddleware``'s source rather than asserted here -- this suite cannot
    reproduce it without an urlconf where ``/admin`` is absent and ``/admin/``
    is present.
    """
    assert get("/admin", country="GB").status_code == 404
    assert get("/admin/login/", country="GB").status_code == 404
