"""Views for the query guard's end-to-end tests.

Four endpoints, in pairs: one that N+1s and one that does not, in both the sync
and the async flavour. The async pair is what pins the module docstring's claim
that ``async`` views are covered.
"""

from asgiref.sync import sync_to_async
from django.http import HttpResponse
from django.urls import path

from tests.queryguard_app.models import Book


def _titles_with_authors():
    return ", ".join(f"{book.title}/{book.author.name}" for book in Book.objects.all())


def _titles_with_authors_eagerly():
    return ", ".join(f"{book.title}/{book.author.name}" for book in Book.objects.select_related("author"))


def nplusone(request):
    return HttpResponse(_titles_with_authors())


def eager(request):
    return HttpResponse(_titles_with_authors_eagerly())


async def async_nplusone(request):
    return HttpResponse(await sync_to_async(_titles_with_authors)())


async def async_eager(request):
    return HttpResponse(await sync_to_async(_titles_with_authors_eagerly)())


urlpatterns = [
    path("nplusone/", nplusone),
    path("eager/", eager),
    path("async-nplusone/", async_nplusone),
    path("async-eager/", async_eager),
]
