"""URLs for the admin geo gate's end-to-end tests.

Four paths, each earning its place:

``/``
    the public site, which the gate must never touch.
``/admin/login/``
    a gated path that *exists*, so a refusal can be distinguished from a miss.
``/admin``
    the slashless form of the prefix. It resolves here so that an *allowed*
    country gets a 200 — which is what makes the refused case's 404 mean the
    gate matched rather than the URL simply not existing.
``/administrator/``
    a path that starts with the prefix's letters but is not under it. Without
    this, a gate implemented as ``path.startswith("/admin")`` passes every test.
"""

from django.http import Http404, HttpResponse
from django.urls import path


def public(request):
    return HttpResponse("PUBLIC-SITE-BODY")


def admin_login(request):
    return HttpResponse("ADMIN-LOGIN-BODY")


def admin_root(request):
    return HttpResponse("ADMIN-ROOT-BODY")


def administrator(request):
    return HttpResponse("ADMINISTRATOR-BODY")


def bare_404(request):
    """Raises an argument-free ``Http404``, exactly as the middleware does.

    This -- not a non-resolving URL -- is the equivalence class a refusal
    belongs to. A non-resolving URL raises ``Resolver404``, which a 404 template
    rendering ``{{ exception }}`` prints differently.
    """
    raise Http404


def leaky_404(request):
    """Raises ``Http404`` *with* a message, as the reachability control.

    An absence assertion over a 404 body is worthless unless something can put
    a string into that body. This view is the something.
    """
    raise Http404("LEAKY-SENTINEL-abc123")


urlpatterns = [
    path("", public),
    path("admin/login/", admin_login),
    path("admin", admin_root),
    path("administrator/", administrator),
    path("admin/leaky/", leaky_404),
    path("admin/bare/", bare_404),
]
