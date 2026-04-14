# abi-django-utils

Shared Django utilities for Tom Clancy's projects ("A Better Internet" pattern library).

This package exists to DRY up common patterns across multiple Django projects: Inventory, Hey Dover, Seacoast Indivisible, etc. Rather than copy-pasting the same settings blocks, install this and call the helpers.

## Installation

```bash
# Install from GitHub (until published to PyPI)
uv add git+https://github.com/tclancy/abi-django-utils

# With development tools (DjDT + nplusone)
uv add "abi-django-utils[dev-tools] @ git+https://github.com/tclancy/abi-django-utils"
```

## Modules

### `abi_django_utils.performance`

Development performance tooling: Django Debug Toolbar + nplusone N+1 detection.

```python
# settings.py
from abi_django_utils.performance import configure_debug_apps, configure_debug_middleware

INSTALLED_APPS = [
    # ... your apps ...
]
if DEBUG:
    INSTALLED_APPS += configure_debug_apps()

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # ... your middleware ...
]
MIDDLEWARE = configure_debug_middleware(MIDDLEWARE, DEBUG)

# DjDT: only show toolbar to localhost
INTERNAL_IPS = ["127.0.0.1"]

# nplusone: raise in dev (DEBUG=True), log warning otherwise
NPLUSONE_RAISE = DEBUG
NPLUSONE_LOGGER = "nplusone.logger"
NPLUSONE_LOG_LEVEL = "WARNING"
```

```python
# urls.py
from django.conf import settings
from abi_django_utils.performance import debug_urls

if settings.DEBUG:
    urlpatterns += debug_urls()
```

## Projects Using This Package

- [ ] Inventory (itemshop) — reference implementation, issue #69
- [ ] Hey Dover — planned
- [ ] Seacoast Indivisible — planned
- [ ] Sandy — N/A (not a web app)

## Development

```bash
git clone https://github.com/tclancy/abi-django-utils
cd abi-django-utils
uv sync
uv run pytest
```
