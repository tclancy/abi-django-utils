# abi-django-utils

Shared Django utilities for Tom Clancy's projects ("A Better Internet" pattern library).

Eliminates settings drift across Django apps by providing canonical env helpers, opinionated defaults, and TLS proxy configuration.

## Installation

```bash
# Install from GitHub
uv add git+https://github.com/tclancy/abi-django-utils

# With Sentry support
uv add "abi-django-utils[sentry] @ git+https://github.com/tclancy/abi-django-utils"
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
