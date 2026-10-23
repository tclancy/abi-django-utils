"""URLs for the admin geo gate's end-to-end tests.

Four paths, each earning its place:

``/``
    the public site, which the gate must never touch.
``/admin/login/``
    a gated path that *exists*, so a refusal can be distinguished from a miss.
``/admin``
    the slashless form of the prefix, which ``APPEND_SLASH`` would otherwise
    answer with a redirect decided before any country was read.
``/administrator/``
    a path that starts with the prefix's letters but is not under it. Without
    this, a gate implemented as ``path.startswith("/admin")`` passes every test.
"""

from django.http import HttpResponse
from django.urls import path


def public(request):
    return HttpResponse("PUBLIC-SITE-BODY")


def admin_login(request):
    return HttpResponse("ADMIN-LOGIN-BODY")


def admin_root(request):
    return HttpResponse("ADMIN-ROOT-BODY")


def administrator(request):
    return HttpResponse("ADMINISTRATOR-BODY")


urlpatterns = [
    path("", public),
    path("admin/login/", admin_login),
    path("admin", admin_root),
    path("administrator/", administrator),
]
