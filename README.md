# abi-django-utils

Shared Django utilities for Tom Clancy's projects ("A Better Internet" pattern library).

Eliminates settings drift across Django apps by providing canonical env helpers, opinionated
defaults, and TLS proxy configuration — plus shared test machinery that would otherwise be
copy-pasted per project.

## Installation

```bash
# Install from GitHub
uv add git+https://github.com/tclancy/abi-django-utils

# With Sentry support
uv add "abi-django-utils[sentry] @ git+https://github.com/tclancy/abi-django-utils"

# With Authelia OIDC support
uv add "abi-django-utils[oidc] @ git+https://github.com/tclancy/abi-django-utils"

# With django-axes login brute-force protection
uv add "abi-django-utils[axes] @ git+https://github.com/tclancy/abi-django-utils"
```

## Usage

```python
# settings.py
from abi_django_utils import env, defaults, tls_proxy, sentry

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="dev-insecure-key-do-not-use-in-production")

locals().update(defaults.django_defaults(debug=DEBUG))
locals().update(tls_proxy.cloudflare_tunnel_defaults(allowed_hosts=ALLOWED_HOSTS, debug=DEBUG))
sentry.init(debug=DEBUG)

# app-specific overrides below
```

## Modules

### `env` — Environment variable helpers

- `env.str(name, default="")` — read a string
- `env.bool(name, default=False)` — read a boolean (truthy: `1`, `true`, `yes`, `on`)
- `env.int(name, default=0)` — read an integer
- `env.list(name, default=None)` — read a comma-separated list (strips whitespace, drops empties)

### `defaults` — Opinionated Django settings

`defaults.django_defaults(debug=True)` returns a dict with:

- `LANGUAGE_CODE`, `TIME_ZONE`, `USE_I18N`, `USE_TZ`
- `DEFAULT_AUTO_FIELD` (BigAutoField)
- `STORAGES` (WhiteNoise in prod, default in dev)
- `AUTH_PASSWORD_VALIDATORS` (all four standard validators, including
  `CommonPasswordValidator` — see [`password_audit`](#password_audit--dictionary-audit-of-admin-account-passwords))
- `LOGGING` (console handler, respects `DJANGO_LOG_LEVEL` env var)

### `password_audit` — dictionary audit of admin-account passwords

Answers "does any account that can reach `/admin/` have a guessable
password?", repeatably, for every app that installs the library.

Add the library to `INSTALLED_APPS` — Django finds management commands by
walking installed apps, so importing the helpers is not enough:

```python
INSTALLED_APPS = [
    ...,
    "abi_django_utils",
]
```

Then, for the **first** audit of an app, test the whole list:

```bash
python manage.py audit_admin_passwords --limit 0
```

That is the run that answers the question. It is also the slow one — see the
cost note below — so for a repeat run or a CI cadence the default samples the
most common 1,000 instead:

```bash
python manage.py audit_admin_passwords

# or bring your own list, gzipped or plain (compression is detected by
# content, so the filename does not have to end in .gz)
python manage.py audit_admin_passwords --password-file /path/to/list.txt.gz
```

Exits non-zero and names the failing accounts as `pk:username` when any
account's password is in the list. It never prints, logs or returns the
password that matched: `password_is_common()` takes a *hash* and returns a
`bool`, so there is no plaintext in scope for a caller to render by accident.
It also refuses to exit 0 having loaded no candidates — a typo'd
`--password-file` is a failure, not a clean bill of health.

**Read a clean run carefully.** The command prints the caveat itself on every
run, and it is not boilerplate:

- A dictionary test can prove an account weak. It can never prove one strong.
- Django's shipped list is **lowercased**, so `Password1` reads clean here —
  even though `CommonPasswordValidator` lowercases the submitted password and
  *would* refuse it. The preventive half below catches a strictly wider set
  than the audit half.
- Coverage is whatever `--limit` allowed, and the command says when it
  truncated. The list is ordered most-common-first, so the default is the top
  1,000 and not an arbitrary slice.

**Cost.** One hash verification is the irreducible unit and it is deliberately
expensive, so this scales with `candidates × accounts`. Measured on Django
6.0.4 (PBKDF2-SHA256, 1.2M iterations) a check is ~190 ms on an M-series Mac,
making its 19,640-entry list ~63 min **per admin account**; Django 4.2 ships
20,000 entries at 600k iterations, so roughly half. Nothing here assumes
either figure — the command measures the real cost against one of your own
hashes and prints the estimate before it starts, and writes each finding as it
is found rather than at the end.

The preventive half is already in `defaults.django_defaults()` — its
`AUTH_PASSWORD_VALIDATORS` includes `CommonPasswordValidator`. An app using the
`locals().update(defaults.django_defaults(debug=DEBUG))` pattern above is
covered; an app that writes its own `AUTH_PASSWORD_VALIDATORS` list may not be.

### `tls_proxy` — TLS reverse proxy settings

`tls_proxy.cloudflare_tunnel_defaults(allowed_hosts, debug=True)` returns:

- `CSRF_TRUSTED_ORIGINS` — derived from public hosts in `ALLOWED_HOSTS`
- `SECURE_PROXY_SSL_HEADER` — for `X-Forwarded-Proto`
- `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` — on in production

### `brute_force` — django-axes login brute-force protection

Requires the `[axes]` extra, which pulls `django-axes[ipware]`. **The `ipware` part is load-bearing, not a nicety** — see below.

```python
# settings.py
from abi_django_utils import brute_force

INSTALLED_APPS = brute_force.with_axes_app([
    # ...
    "django.contrib.staticfiles",
])

MIDDLEWARE = brute_force.with_axes_middleware([
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
])

locals().update(brute_force.axes_defaults())
```

`axes` ships models, so run `manage.py migrate` when you add it. Deploy these settings without migrating and the first login attempt — failed *or* successful — raises on a missing `axes_accessattempt` table, which is a login outage rather than the protection you were installing.

`brute_force.axes_defaults()` returns:

| Setting | Default | Notes |
|---|---|---|
| `AUTHENTICATION_BACKENDS` | axes standalone backend, then `ModelBackend` | Pass `backends=[...]` to keep your own; the axes one is always put first |
| `AXES_FAILURE_LIMIT` | `5` | `failure_limit=` |
| `AXES_COOLOFF_TIME` | `1` (hour) | `cooloff_hours=` |
| `AXES_LOCKOUT_PARAMETERS` | `[["ip_address"]]` | |
| `AXES_RESET_ON_SUCCESS` | `True` | |
| `AXES_ONLY_ADMIN_SITE` | `True` | `only_admin_site=False` to also watch a non-admin login |
| `AXES_IPWARE_META_PRECEDENCE_ORDER` | `CF-Connecting-IP`, `X-Forwarded-For`, `REMOTE_ADDR` | omitted when `proxy_aware=False` |

Three helpers compose the list settings, all idempotent and none of which mutate their argument:

- `with_axes_app(installed_apps)` — appends `"axes"`
- `with_axes_middleware(middleware)` — puts `AxesMiddleware` **last**, moving it there if it is already somewhere else; it maps a lockout signal onto the response, so it has to wrap the view
- `with_axes_backend(backends)` — puts `AxesStandaloneBackend` **first**, so it sees the attempt before anything authenticates it

#### Why the `ipware` extra is not optional

`django-axes` resolves the client IP through `django-ipware`, which it declares under its own `ipware` extra. Install bare `django-axes` and two things are true at once, neither of them visible from your settings file:

- `AXES_IPWARE_META_PRECEDENCE_ORDER` is never read. `axes.helpers.get_client_ip_address` falls through to `request.META["REMOTE_ADDR"]`.
- `django-axes`' own default for that setting is `("REMOTE_ADDR",)` anyway — it does **not** read a client-supplied `X-Forwarded-For` by default.

Behind a Cloudflare tunnel `REMOTE_ADDR` is the tunnel gateway, the same address for every visitor. With `AXES_LOCKOUT_PARAMETERS` keyed on `ip_address` that is one shared bucket: five failed logins from anyone lock out everyone, yourself included, and the per-client rate limit you think you installed does not exist.

So `axes_defaults()` raises `ImproperlyConfigured` rather than hand back a precedence order django-axes would ignore. `proxy_aware=False` drops the key and accepts the shared bucket, which is a thing you can choose but not a thing you can get by accident.

`tests/test_brute_force.py` drives `axes.helpers.get_client_ip_address` on a request carrying all three headers and asserts all three directions — with the pin it resolves the `CF-Connecting-IP`, without the pin the gateway, and with ipware absent the gateway again regardless of the pin.

#### What the pin does not protect against

**The precedence order assumes Cloudflare is the only route to the app.** `CF-Connecting-IP` is unspoofable because Cloudflare overwrites it — on traffic that goes through Cloudflare. A request that reaches Django another way sets the header itself and picks its own lockout key, a fresh one per attempt. Measured with the pin active:

| request | axes resolves |
|---|---|
| through the tunnel, `CF-Connecting-IP: 198.51.100.7` | `198.51.100.7` |
| direct, claiming `CF-Connecting-IP: 1.2.3.4` | `1.2.3.4` |
| direct, claiming `X-Forwarded-For: 203.0.113.9` | `203.0.113.9` |
| direct, no headers claimed | the real `REMOTE_ADDR` |

That route is open today: every homelab Django compose template publishes its port as `"{{ service_ports.X }}:8000"` with no `127.0.0.1:` prefix, so Docker binds it on `0.0.0.0` and the app answers on the LAN. For an app that **already** runs axes unpinned, that is the one way adopting this helper is not strictly better — unpinned it keys on the attacker's real `REMOTE_ADDR` and does lock out.

Two places to close it, neither of them this function:

- **the deployment** — bind the published port to loopback, which removes the non-Cloudflare route for every app at once. Preferred, and tracked separately.
- **the library** — `AXES_CLIENT_IP_CALLABLE`, honouring `CF-Connecting-IP` only when `REMOTE_ADDR` is a known tunnel address. That needs a per-deployment trusted-proxy list, so it is a design call rather than a patch; see abi-django-utils#16. (`AXES_IPWARE_PROXY_TRUSTED_IPS` does **not** do this — ipware applies it to the `X-Forwarded-For` chain, not to `CF-Connecting-IP`.)

#### `AXES_ONLY_ADMIN_SITE` defaults to `True`

The exposure this answers is admin-login enumeration, and `True` is the narrowest change for an app adopting axes from nothing: it cannot lock a legitimate user out of a surface it does not watch.

**All three consumers take the default.** django-axes can only observe a login that passes through `django.contrib.auth.authenticate()`, and none of `indivisible`, `recordclub` or `canvasoptimizer` has a non-admin login that does — checked for `authenticate(` and for a login route on each repo's `main`. For them `False` would widen nothing and would give up the narrowing on `/admin/`. Pass `False` for an app that routes a non-admin login through `authenticate()`.

The corollary matters more than the flag: **a login that bypasses `authenticate()` is invisible to django-axes at any setting.** `recordclub`'s magic-link POST at `/auth/login/` is exactly that — it looks a `Member` up by email, mails a token, and keeps its own `request.session["member_id"]`. Rate-limiting it needs a different control; `recordclub` already carries `django-ratelimit`.

### `geo_gate` — Refuse `/admin/` from outside an allowed country set

`geo_gate.AdminGeoGateMiddleware` reads Cloudflare's `CF-IPCountry` header and
404s a configurable path prefix — the admin, by default — for any country not in
`ADMIN_GEO_GATE_COUNTRIES`. No GeoIP database, no new dependency, no dashboard
work.

```python
# settings.py
from abi_django_utils import geo_gate

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "abi_django_utils.geo_gate.AdminGeoGateMiddleware",
    "django.middleware.common.CommonMiddleware",
    # ... session, auth, axes below, so a refused request never reaches them
]

globals().update(geo_gate.admin_geo_gate_defaults())
```

**Above `CommonMiddleware`, deliberately.** Below it, `APPEND_SLASH` turns a
refused `/admin` into a `301` to `/admin/`, which confirms the admin exists —
the reconnaissance the 404 was chosen to avoid.

| Setting | Default | Meaning |
|---------|---------|---------|
| `ADMIN_GEO_GATE_ENABLED` | `True`, from `ADMIN_GEO_GATE_ENABLED` env var | Kill switch. One env flip for a trip abroad, not a deploy. |
| `ADMIN_GEO_GATE_COUNTRIES` | `{"US"}` | Allowed ISO 3166-1 alpha-2 codes, compared upper-cased. |
| `ADMIN_GEO_GATE_PATHS` | `("/admin/",)` | Prefixes to gate. Also matches the slashless form; does **not** match `/administrator/`. |
| `ADMIN_GEO_GATE_REQUIRE_HEADER` | `False` | What to do when the country is unknown. See below. |

**The header does arrive — measured, with a control.** On 2026-10-23 the origin
side of a live public request was captured on the box by sniffing the loopback
socket `cloudflared` forwards to. `recordclub.tomclancy.info` and
`www.greaterseacoastnhindivisible.org` — two different Cloudflare zones — both
delivered `Cf-Ipcountry: US`. In the same capture, a control request issued on
the box straight to `http://localhost:8091/` arrived with **no `Cf-*` header of
any kind.**

**That control is the security model, and the limit.** One non-Cloudflare path
measured once is enough to show the headers are put there by Cloudflare rather
than being some ambient property of a request — and not enough to claim
anything stronger. The containers publish on `0.0.0.0`
([homelab#591](https://github.com/tclancy/homelab/issues/591)), so a caller
already on the LAN reaches the admin directly: the polite version arrives with
no header and is *allowed* under the default policy, and the impolite version
sends `CF-IPCountry: US` itself and is believed. This is defence in depth behind
the tunnel, not a perimeter; the LAN exposure is a deployment fix, not a
middleware one.

**Unknown country is the load-bearing choice.** Missing, empty, or Cloudflare's
own could-not-geolocate `XX` all count as unknown. The default
(`ADMIN_GEO_GATE_REQUIRE_HEADER = False`) **allows and logs a warning** — because
local development, LAN access and a zone with IP Geolocation switched off all
produce an absent header, and a gate that locks you out of your own admin on
first deploy gets removed rather than fixed. The warning is not decorative:
fail-open with a silent log is a gate that stops existing without anyone
noticing. Set it to `True` once you have confirmed the header reaches every path
you serve the admin on.

`T1` (Cloudflare's code for Tor) is deliberately *not* unknown — it is an
answer, just not an allowed one, so it is refused by the ordinary rule
regardless of `ADMIN_GEO_GATE_REQUIRE_HEADER`.

**Refusals raise a bare `Http404`.** Not 403 — that confirms the path exists and
that something is gating it. Raising rather than returning lets Django render
the *application's own* 404, so a refusal is byte-identical to any
application-raised 404. The `Http404` carries no message, because Django's
`page_not_found` surfaces `exception.args[0]` and `DEBUG`'s technical 404 page
prints it — the one place an explanation would leak. The reason goes to the log
instead.

Three distinguishers remain, all pinned by tests rather than claimed away: the
refusal carries no `Content-Length` (`CommonMiddleware` is below it); a 404
template that renders `{{ exception }}` prints `Http404` where a non-resolving
URL prints `Resolver404`, so it reveals that `/admin/*` is special; and under
`DEBUG` the bodies differ anyway. None of the three says *country*, *geo* or
*Cloudflare*, which is the property that matters.

**Settings may be a bare string.** `ADMIN_GEO_GATE_PATHS = ("/admin/")` is a
missing comma away from a 1-tuple and would otherwise iterate as the characters
`/`, `a`, `d`… — gating the entire public site on the first one. Both this and
`ADMIN_GEO_GATE_COUNTRIES = "US"` (which would otherwise allow nobody) are
coerced, and both cases are tested.

**Prefixes are URLconf-relative.** Matching is on `request.path_info`, not
`request.path`, so a `FORCE_SCRIPT_NAME` or WSGI-mounted app does not need its
prefixes rewritten — and more to the point, does not silently stop being gated.

**It composes with a Cloudflare WAF rule rather than replacing one.** A Custom
Rule (`http.request.uri.path contains "/admin" and ip.geoip.country ne "US"`,
action `Managed Challenge`) blocks at the edge and is strictly better, but needs
doing by hand per zone and cannot be shipped from here — the only Cloudflare
token in the homelab vault is DNS-01 scoped. Having both means the middleware
still holds if a rule is ever removed.

### `sentry` — Sentry error monitoring

`sentry.init(debug=True, traces_sample_rate=0.1)` — initializes Sentry from `SENTRY_DSN` env var. No-op when `debug=True` or DSN is empty.

### `oidc` — Authelia OIDC via `mozilla-django-oidc`

Requires the `[oidc]` extra. Adds Authelia single sign-on to a Django app as an **additional** login option — the password form at `/accounts/login/` stays the default; `oidc.configure()` deliberately does not set `LOGIN_URL`.

```python
# settings.py
from abi_django_utils import env, defaults, tls_proxy, oidc, sentry

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="dev-insecure-key")

INSTALLED_APPS = [
    # ...
    "mozilla_django_oidc",
]

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",              # primary — password login still works
    "abi_django_utils.oidc.AutheliaOIDCBackend",              # appended — OIDC as additional option
)

locals().update(defaults.django_defaults(debug=DEBUG))
locals().update(tls_proxy.cloudflare_tunnel_defaults(allowed_hosts=ALLOWED_HOSTS, debug=DEBUG))
locals().update(oidc.configure(
    issuer_url=env.str("OIDC_ISSUER_URL", default="https://auth.tomclancy.info"),
    client_id=env.str("OIDC_CLIENT_ID"),
    client_secret=env.str("OIDC_CLIENT_SECRET"),
))
sentry.init(debug=DEBUG)
```

```python
# urls.py
urlpatterns = [
    path("oidc/", include("mozilla_django_oidc.urls")),
    # ... app urls
]
```

**Additive login template snippet** — put on your login page so users can choose:

```django
{# templates/registration/login.html — password form goes above #}
<form method="post">{% csrf_token %}{{ form.as_p }}<button type="submit">Sign in</button></form>

<p>Or <a href="{% url 'oidc_authentication_init' %}">sign in with Authelia</a></p>
```

Retire password login on a given app later (if you want) with one line: `LOGIN_URL = "/oidc/authenticate/"` in that app's `settings.py`. The library never makes that decision for you.

#### Behavior

- **Case-insensitive email link.** OIDC user → Django user matches by `email__iexact`. `Tom@Example.com` at Authelia and `tom@example.com` in Django link to the same user, no duplicate.
- **First-login provisioning.** Unknown email → `create_user()` with the lowercased email and `preferred_username` (or the local-part of the email) as the username.
- **`oidc:`-namespaced group sync.** Authelia `groups` claim `["admins"]` → Django group `oidc:admins`. Groups without the `oidc:` prefix (e.g. `staff`, per-app permission groups) are never touched — safe to mix local-only groups with IdP-managed groups.
- **RP-initiated logout.** Django `logout()` calls Authelia's `/api/oidc/logout` with the stored `id_token_hint` so the SSO session ends too. Otherwise a "logged-out" user hits any protected view and is silently re-authenticated.
- **`RS256` signing algo.** `mozilla-django-oidc` defaults to `HS256`; Authelia signs with RSA. `configure()` sets `RS256` explicitly.

#### Homelab-side (Authelia)

Add a client block to `authelia-configuration.yml.j2` for each Django app:

```yaml
- client_id: 'heydover'
  client_name: 'Hey Dover'
  client_secret: '{{ authelia_heydover_client_secret_hash }}'
  public: false
  authorization_policy: 'two_factor'
  redirect_uris:
    - 'https://heydover.tomclancy.info/oidc/callback/'
  scopes: ['openid', 'profile', 'email', 'groups']
  response_types: ['code']
  grant_types: ['authorization_code']
  token_endpoint_auth_method: 'client_secret_post'
  userinfo_signed_response_alg: 'none'
```

Two vault entries per app: `vault_<app>_oidc_client_secret` (plaintext, mapped into the app's env) and `vault_authelia_<app>_client_secret_hash` (PBKDF2 hash, into Authelia config). Seed both with `openssl rand -hex 32` + `docker run --rm authelia/authelia:latest authelia crypto hash generate pbkdf2 --password <plaintext>`.

#### Swapping to `django-allauth` later

If a specific project needs `django-allauth` (e.g. adding more social providers), the swap is ~10 minutes and reuses the same `User` rows — both libraries link the local user by email. Replace the 3 `abi_django_utils.oidc` settings lines with `django-allauth`'s `SOCIALACCOUNT_PROVIDERS` block, swap `urls.py` `/oidc/` include for `/accounts/`, and update the Authelia `redirect_uris` to `/accounts/oidc/authelia/login/callback/`. See [issue #8](https://github.com/tclancy/abi-django-utils/issues/8) discussion for the full recipe.

### `queryguard` — Blanket N+1 query detection for test suites

Catches N+1 queries across an **entire** test suite with no per-test
annotation. Needs no extra, no third-party dependency, and no runner migration:
it is built on `connection.execute_wrapper()`, public and stable since Django
2.0, so it cannot break the way `nplusone` did on each ORM internals change.

An N+1 is one query *shape* run repeatedly inside one unit of work. The guard
fingerprints every `SELECT` before parameters are interpolated and flags any
shape that repeats inside one window, so the report names the offending query
rather than just counting queries.

```python
# settings.py — blanket, across everything
TEST_RUNNER = "abi_django_utils.queryguard.QueryGuardRunner"
```

```python
# or one class at a time, leaving the rest of the suite alone
from abi_django_utils.queryguard import QueryGuardMixin

class ItemListTests(QueryGuardMixin, TestCase):
    ...
```

| Setting | Default | Meaning |
|---------|---------|---------|
| `QUERY_GUARD_REPORT_ONLY` | `True` | Collect findings and print a report at the end of the run instead of failing tests |
| `QUERY_GUARD_MAX_REPEATS` | `1` | How many times one shape may appear in one window before it is a finding |
| `QUERY_GUARD_BASELINE` | *(none)* | Path to the baseline file. Unset means no baseline: nothing is excused |

**Report-only is the default on purpose.** Switching a blanket detector on over
an existing suite finds real N+1s, and a library that turns your suite red on
install gets uninstalled. Read the report, baseline what it names, then set
`QUERY_GUARD_REPORT_ONLY = False` to make it a gate.

#### The baseline — how you enforce on a suite you have not triaged

A blanket detector on an existing suite leaves two bad options — stay in
report-only, where nobody reads it, or clear every finding before anything is
protected — and one good one. The **baseline** is a committed record of what each
test *already* repeats, so the guard fails on anything new from the day it lands.

```python
QUERY_GUARD_BASELINE = BASE_DIR / "queryguard_baseline.json"
QUERY_GUARD_REPORT_ONLY = False
```

```bash
QUERY_GUARD_UPDATE_BASELINE=1 manage.py test   # writes the file; commit it
```

```json
{
  "shop.tests.DashboardTests.test_lists_orders": {
    "SELECT \"shop_customer\".\"id\", ... WHERE \"id\" = %s LIMIT 21": 4
  }
}
```

- **Per query shape, not per test.** An entry raises the allowance for the query
  that earned it and nothing else; a shape it does not name is judged as strictly
  as in a test with no entry at all. A per-*test* allowance is a hole, not a
  ratchet — a brand-new 2x repeat of an unrelated query lands inside it silently.
- **It only tightens.** A baselined test that gets worse still fails. Fix one and
  regenerating drops the entry; there is no way to loosen it except by hand, and
  hand-editing is the one abuse this design cannot prevent.
- **Regenerating is not optional.** An enforcing run that proves an entry is no
  longer earned **fails**, naming the entries. Otherwise a fix tightens the ratchet
  without the file following, and the next PR to regenerate ships deletions nobody
  in it caused. Report-only mode keeps that as a note.
- **The baseline needs `QueryGuardRunner`.** Writing it, carrying entries, and the
  stale gate all live on the runner, so none of them happen under `QueryGuardMixin`
  alone. Setting `QUERY_GUARD_UPDATE_BASELINE` with no runner active says so on
  stderr rather than quietly collecting instead of enforcing.
- **Readable in a diff**, which is the whole value of the file: sorted, indented,
  and naming the SQL, so a review sees *which* query a fix removed.
- **No default path**, deliberately. This module lives in your virtualenv, so a
  path derived from its own location would write a generated artifact somewhere
  unreviewable that the next `uv sync` deletes. Unset means no baseline, which is
  byte-identical to the behaviour without this feature.
- **Two refusals** stop an update run that looks like it worked: under `--parallel`,
  where findings live in worker processes nothing gathers back, and from a run that
  bracketed no tests, which would blank an existing file. A blanked baseline is a
  *passing* suite until the next real regeneration.
- **A partial run carries what it did not measure.** `manage.py test shop.tests`
  keeps every other entry and says how many it carried. Deleting them reads in
  review exactly like the ratchet tightening while disarming the whole rest of the
  suite.

#### Two decorators, and which one you want

```python
from abi_django_utils.queryguard import allow_repeats, expect_repeats

@allow_repeats(5)           # the repetition is INTENDED
def test_pagination_walks_every_page(self):
    ...

@expect_repeats(4)          # a known N+1 you are not fixing today
def test_the_dashboard_still_queries_per_row(self):
    ...
```

`@allow_repeats` is a permanent exemption. The day somebody fixes the N+1 it was
covering, it stays, the ceiling stays raised, and a new repeat of any shape in that
test is silently inside it.

`@expect_repeats` is the same allowance **plus an assertion that it is still
needed** — fix the N+1 and the test fails, naming the decorator to remove.

The pin is **exact in both directions**: the count is the allowance as well as the
assertion, so a repeat that gets *deeper* fails too, with a message naming this
decorator rather than telling you to add `@allow_repeats`. A change in either
direction is worth a reader's attention.

It counts repeats rather than shapes, so it cannot tell "this N+1" from "an N+1 of
the same depth"; for shape-level pinning use the baseline, which records the SQL.
The two compose: a baselined test may also carry the decorator. Unlike a blanket
finding, `@expect_repeats` fails in report-only mode too — it is an assertion you
wrote by hand about one test, not a detector sweeping an untriaged suite.

#### Units of work

Counting is scoped to a **window**, because that is what an N+1 lives inside: a
test making two HTTP requests repeats the first's queries by design, and
charging that to the second would flag correct code. Requests are bracketed
automatically, at both ends.

Anything Django does not signal — a Celery task body, a management command, a
service function called directly — is one window per test unless you say
otherwise, which makes a loop over two of them look like an N+1 of every shape
they share. Bracket those:

```python
from abi_django_utils.queryguard import new_window

for tenant in tenants:
    with new_window():
        call_command("rebuild_index", tenant=tenant.id)
```

`new_window()` is a no-op when no guard is active, so a helper shared between a
test and a management command can call it unconditionally. Don't import this
module *from* production code, though — it pulls `django.test.runner` and
registers an `atexit` hook at import.

#### What is and is not covered

`execute_wrapper` is registered on the connections *this process* holds, and
Django's `connections` is thread-local. ORM work on another thread therefore uses
a connection the guard never wrapped.

**The dividing line is how the event loop is entered, not sync versus async.**
`sync_to_async`'s default `thread_sensitive=True` means "run on the thread the
outer *synchronous* caller is on" — and with no outer synchronous caller it falls
through to a shared single-worker thread pool instead. Measured on
asgiref 3.11 / Django 6.0:

| entry point | thread-sensitive work runs on | covered |
|---|---|---|
| `async_to_sync(coro)()` | `MainThread` | yes |
| Django `async def test_*` | `MainThread` | yes |
| `asyncio.run(coro)` | pool worker | **no** |
| `asyncio.run` + `ThreadSensitiveContext` | pool worker | **no** |

**Covered:**

- Sync tests and sync views.
- `async` views through `AsyncClient`, and Django's async ORM (`async for`,
  `aget`) inside them.
- Django's own `async def test_*` methods — `SimpleTestCase` wraps them in
  `async_to_sync` before calling them, which is what keeps them on the main thread.
- Every configured database alias.
- `--parallel` workers. A `spawn`-started worker inherits no monkeypatch, so the
  runner installs the guard inside each one; if Django's private worker hook ever
  moves, `--parallel` is **refused at construction** rather than silently running
  unguarded.

**Not covered — these pass silently:**

- A bare `asyncio.run()` / `run_until_complete()` in a test body, and
  `pytest-asyncio`.
- `unittest.IsolatedAsyncioTestCase`. It overrides `_callTestMethod`, so the
  blanket patch never applies and the guard is not merely blind but never entered.
- `sync_to_async(..., thread_sensitive=False)` and a bare `threading.Thread`.
- Plain pytest *function* tests under the mixin-less path — the patch targets
  `unittest.TestCase._callTestMethod`, so only `TestCase` subclasses are hooked.

There is no fix available from inside an `execute_wrapper`, so these are
documented and pinned by tests in both directions rather than worked around.

Note that **SQLite does not save you here**: Django rewrites a `":memory:"` test
database to `file:memorydb_<alias>?mode=memory&cache=shared`, so another thread
shares the same tables rather than finding an empty database.

#### Two more things worth knowing before you adopt

**`QueryGuardRunner` needs Django's test runner.** A project on pytest cannot set
`TEST_RUNNER` and get blanket coverage; use `QueryGuardMixin`, which works under
any runner that goes through `unittest.TestCase`.

**Generate your first report serially.** Findings live in a module-global list,
which is per *process*. `--parallel` workers are separate processes and nothing
gathers their lists back, so report-only mode under `--parallel` prints a partial
report per worker at pool teardown rather than one consolidated report. Enforcing
mode is unaffected — failures travel home in the test result.

#### Not yet extracted

The prototype in `itemshop` also carries a **baseline ratchet** — a committed
per-shape record of what each test already repeats, so the guard fails on
anything *new* from the day it lands. That is the piece that makes enforcement
practical on a large existing suite without triaging every finding first, and it
is a separate slice.

## Env Var Conventions

All env vars use the `DJANGO_` prefix:

| Setting | Env Var | Format |
|---------|---------|--------|
| Debug mode | `DJANGO_DEBUG` | `true`/`false` |
| Allowed hosts | `DJANGO_ALLOWED_HOSTS` | Comma-separated |
| Secret key | `DJANGO_SECRET_KEY` | String |
| Log level | `DJANGO_LOG_LEVEL` | Python log level name |
| Sentry DSN | `SENTRY_DSN` | Full Sentry DSN URL |
| Admin geo gate | `ADMIN_GEO_GATE_ENABLED` | `true`/`false` (unset = on) |

## Agent's Understanding

This library addresses settings drift across SI, hey-dover, and recordclub:
- Three different env var names for `ALLOWED_HOSTS` (with/without `DJANGO_` prefix)
- Three different list separators (comma, whitespace, comma-no-strip)
- CSRF origins: derived in two apps, separate env var in one
- Missing `SECURE_PROXY_SSL_HEADER` and secure cookies in two of three apps
- Logging config copy-pasted with slight divergences

**Why a library instead of a template:** Templates drift immediately. A library is importable, testable, and version-pinnable. When the convention changes, bump the version and all apps get the update.

**Why two-module split (defaults + tls_proxy):** The deployment substrate (Cloudflare Tunnel today) is swappable. If an app moves off the homelab, it keeps `defaults` and swaps `tls_proxy`.

**`brute_force` is the same argument one layer down.** Two apps ran django-axes and disagreed on `AXES_IPWARE_META_PRECEDENCE_ORDER` — a setting whose wrong value is invisible, because the app still looks protected while the lockout keys on the tunnel gateway instead of the client. That is drift a shared default fixes and a template does not.

## Projects Using This Package

- [ ] Seacoast Indivisible — reference implementation (PR #14 converged manually, ready for library)
- [ ] Hey Dover — needs migration PR (whitespace→comma separator, add proxy settings)
- [ ] Record Club — needs migration PR (rename env vars, add proxy settings)
- [ ] Inventory — planned

## Development

```bash
git clone https://github.com/tclancy/abi-django-utils
cd abi-django-utils
uv sync
uv run pytest
```

## Code Review Gate

Pull requests here are graded by two GitHub Actions workflows plus one jq
program. If you are looking at a PR with a yellow `dispatch/require-review`,
this section is why.

| File | Job |
|------|-----|
| `.github/workflows/claude-code-review.yml` | Runs the reviewer and posts findings as inline comments. Fires on `opened` / `reopened`, and on a comment starting with `/review`. Never on `synchronize` — a 10-commit PR would otherwise fire 10 reviews. |
| `.github/workflows/require-review.yml` | Publishes the `dispatch/require-review` commit status to the PR **head**, saying whether a review exists *for that head*. |
| `.github/review-gate.jq` | The decision, as a pure function. JSON in, `{state, description}` out. Unit-tested by `tests/test_review_gate.py`. |

### The three states you will see

- **green** — a review exists for the current head commit.
- **red** — either there is no review, or every review predates the head. Both
  refusals name the remedy: comment `/review` on the PR and the reviewer runs
  again, re-publishing the status.
- **yellow / pending** — the gate is not provisioned (see below). Deliberately
  not green: a reviewer that cannot run must not read like one that passed.

This status blocks nothing. There are no rulesets on this repo; the red X is
backpressure for the autonomous agents that open most PRs here.

### Provisioning (two steps, both repo-admin)

The gate ships unprovisioned and says so. To turn it on:

1. Add a `CLAUDE_CODE_OAUTH_TOKEN` repository secret
   (*Settings → Secrets and variables → Actions*).
2. Install the **Claude GitHub App** on this repository.

Step 2 is easy to miss and is just as load-bearing as step 1: the gate matches
on the comment author `claude[bot]`, which only exists once the App is
installed. With the secret but not the App, the reviewer's comments arrive
under a different login, the gate never matches them, and the PR sits red with
a remedy that cannot work.

### Outside contributors

Cross-repository (fork) PRs are skipped by both workflows and get a human
review instead. Actions cannot post inline comments back across the fork
boundary, and on the `pull_request` trigger a fork PR's token cannot write a
commit status either — so a gate there would be a red X with no available
remedy.
