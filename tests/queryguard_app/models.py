"""Two models with one FK between them — the smallest shape an N+1 needs.

Defined in a test-only app rather than borrowed from ``django.contrib`` so the
N+1 under test is unambiguous: ``Book.author`` is a plain forward FK with no
manager-level cache in front of it, so iterating books and touching
``book.author`` fires exactly one extra ``SELECT`` per row. The app ships no
migrations; Django's ``create_test_db`` runs ``migrate --run-syncdb``, which
creates the tables.
"""

from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "queryguard_app"


class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")

    class Meta:
        app_label = "queryguard_app"
