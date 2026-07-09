# abi-django-utils

Shared Django utilities for Tom Clancy's projects ("A Better Internet" pattern library).

Eliminates settings drift across Django apps by providing canonical env helpers, opinionated defaults, and TLS proxy configuration.

## Installation

```bash
# Install from GitHub
uv add git+https://github.com/tclancy/abi-django-utils

# With Sentry support
uv add "abi-django-utils[sentry] @ git+https://github.com/tclancy/abi-django-utils"

# With Authelia OIDC support
uv add "abi-django-utils[oidc] @ git+https://github.com/tclancy/abi-django-utils"
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
- `AUTH_PASSWORD_VALIDATORS` (all four standard validators)
- `LOGGING` (console handler, respects `DJANGO_LOG_LEVEL` env var)

### `tls_proxy` — TLS reverse proxy settings

`tls_proxy.cloudflare_tunnel_defaults(allowed_hosts, debug=True)` returns:

- `CSRF_TRUSTED_ORIGINS` — derived from public hosts in `ALLOWED_HOSTS`
- `SECURE_PROXY_SSL_HEADER` — for `X-Forwarded-Proto`
- `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` — on in production

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

## Env Var Conventions

All env vars use the `DJANGO_` prefix:

| Setting | Env Var | Format |
|---------|---------|--------|
| Debug mode | `DJANGO_DEBUG` | `true`/`false` |
| Allowed hosts | `DJANGO_ALLOWED_HOSTS` | Comma-separated |
| Secret key | `DJANGO_SECRET_KEY` | String |
| Log level | `DJANGO_LOG_LEVEL` | Python log level name |
| Sentry DSN | `SENTRY_DSN` | Full Sentry DSN URL |

## Agent's Understanding

This library addresses settings drift across SI, hey-dover, and recordclub:
- Three different env var names for `ALLOWED_HOSTS` (with/without `DJANGO_` prefix)
- Three different list separators (comma, whitespace, comma-no-strip)
- CSRF origins: derived in two apps, separate env var in one
- Missing `SECURE_PROXY_SSL_HEADER` and secure cookies in two of three apps
- Logging config copy-pasted with slight divergences

**Why a library instead of a template:** Templates drift immediately. A library is importable, testable, and version-pinnable. When the convention changes, bump the version and all apps get the update.

**Why two-module split (defaults + tls_proxy):** The deployment substrate (Cloudflare Tunnel today) is swappable. If an app moves off the homelab, it keeps `defaults` and swaps `tls_proxy`.

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
