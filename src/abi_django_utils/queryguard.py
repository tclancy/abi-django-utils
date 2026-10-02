"""Catch N+1 queries across a whole Django test suite, with no per-test annotation.

An N+1 is one query shape run repeatedly within a single unit of work: a page
that fetches a list of rows and then fires another query per row. The reason
they reach production is that they are invisible at test-fixture scale — three
rows means four queries, which looks like nothing — and then the cost grows with
real data.

This spots them by fingerprinting every ``SELECT`` and flagging any shape that
repeats inside one window. Because the fingerprint is the SQL as Django wrote
it, *before* parameters are substituted, the report names the offending query
rather than just counting it — and no SQL parser is needed to get it.

Why not an existing package
---------------------------

``nplusone`` is abandoned (2018) and breaks on modern Django because it
monkey-patches ORM internals. ``django-perf-rec`` and ``inline-snapshot-django``
both work, but both are **opt-in per test**: you only catch N+1s in tests
somebody remembered to wrap. The gap this fills is blanket coverage over an
existing suite with no annotation, built on public Django APIs only
(``connection.execute_wrapper``, stable since 2.0) so it cannot break the same
way.

Turning it on
-------------

Across the whole suite, with no changes to any test::

    manage.py test --testrunner=abi_django_utils.queryguard.QueryGuardRunner

or permanently, in settings::

    TEST_RUNNER = "abi_django_utils.queryguard.QueryGuardRunner"

For one test class, leaving the rest of the suite alone::

    from abi_django_utils.queryguard import QueryGuardMixin

    class ItemListTests(QueryGuardMixin, TestCase):
        ...

Settings
--------

``QUERY_GUARD_REPORT_ONLY`` (default ``True``)
    Collect findings and print a report at the end of the run instead of
    failing tests. **The default is report-only on purpose**: switching a
    blanket detector on over an existing suite finds real N+1s, and a library
    that turns an adopter's suite red on install gets uninstalled. Read the
    report, fix or ``@allow_repeats`` what it names, then set this to ``False``
    to make it a gate.

``QUERY_GUARD_MAX_REPEATS`` (default ``1``)
    How many times one shape may appear in a window before it is a finding.
    ``1`` means a shape seen twice in one window is a finding, which is the
    sensitivity that catches a real N+1 over three fixture rows.

Per test, for the genuinely deliberate cases::

    @allow_repeats(5)
    def test_pagination_walks_every_page(self):
        ...

Units of work
-------------

Counting is scoped to a **window**, because an N+1 lives inside one unit of
work: a test making two HTTP requests repeats the first's queries by design,
and charging that to the second would flag correct code.

Requests are bracketed automatically, via ``request_started`` *and*
``request_finished``. Anything Django does not signal — a Celery task body, a
management command, a service function called directly — is one window per test
unless you say otherwise, which makes a loop over two commands look like an
N+1. Bracket those explicitly::

    from abi_django_utils.queryguard import new_window

    for tenant in tenants:
        with new_window():
            call_command("rebuild_index", tenant=tenant.id)

Known limitation: off-main-thread ORM work
------------------------------------------

``execute_wrapper`` is registered on the connections this process holds, and
Django's ``connections`` is thread-local. ORM work that runs on **another**
thread therefore uses a connection the guard never wrapped, and is invisible to
it. In practice that means:

* ``async`` views, ``AsyncClient``, and Django's own async ORM (``async for``,
  ``acreate``) **are** covered — asgiref's default thread-sensitive executor
  runs them on the main thread, which is where the wrapper is. Measured, and
  pinned by ``AsyncCoverageTests``.
* ``sync_to_async(..., thread_sensitive=False)`` and a bare
  ``threading.Thread`` are **not** covered.

On SQLite the uncovered case usually announces itself (a separate in-memory
database has no tables); on PostgreSQL it is silent. There is no fix available
from inside a wrapper, so this is documented rather than worked around.
"""

from __future__ import annotations

import atexit
import sys
import threading
import unittest
from collections import Counter
from contextlib import ExitStack, contextmanager

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import request_finished, request_started
from django.db import connections
from django.test.runner import DiscoverRunner, ParallelTestSuite

__all__ = [
    "QueryCollector",
    "QueryGuardMixin",
    "QueryGuardRunner",
    "allow_repeats",
    "emit_report",
    "format_finding",
    "format_report",
    "guarding",
    "install",
    "is_select",
    "new_window",
]

#: Findings accumulated across the run while in report-only mode.
_FINDINGS: list[tuple[str, dict[str, int]]] = []

#: Collectors currently recording, so ``new_window()`` can reach them without
#: being handed one. A list rather than a single slot because ``guarding()`` is
#: re-entrant and the guard's own tests build collectors directly; under normal
#: use it holds at most one. Guarded by ``_ACTIVE_LOCK`` because a thread that
#: cannot be *measured* (see the module docstring) can still call
#: ``new_window()``.
_ACTIVE: list[QueryCollector] = []
_ACTIVE_LOCK = threading.Lock()

#: Reported at most once per process — see ``warm_content_types``.
_WARM_FAILURE_REPORTED = False


def allow_repeats(count: int):
    """Raise the repeated-shape threshold for a single test.

    For the genuinely deliberate cases — a test that loops on purpose to prove
    pagination, say — rather than as a way to quiet a real finding.
    """

    def decorator(func):
        func._allow_repeats = count
        return func

    return decorator


def report_only() -> bool:
    """Whether findings are collected and printed instead of failing tests."""
    return getattr(settings, "QUERY_GUARD_REPORT_ONLY", True)


def default_threshold() -> int:
    """How many times one shape may appear in a window before it is a finding."""
    return getattr(settings, "QUERY_GUARD_MAX_REPEATS", 1)


def is_select(sql: str) -> bool:
    """Whether this statement is a read, and so a candidate for an N+1.

    Reads only: a repeated ``INSERT`` shape is an ordinary bulk write, and
    counting those makes routine fixture setup look like an N+1.
    """
    return sql.lstrip().upper().startswith("SELECT")


class QueryCollector:
    """Records SELECT shapes, banking a fresh window at each boundary.

    Used as a ``connection.execute_wrapper()`` callable.

    Both ends of a request are boundaries, and that is load-bearing. Banking on
    ``request_started`` alone closes a request's window only when the *next*
    request opens, so everything the test body does after its last request is
    charged to that request: a test that hits a page and then re-reads a row the
    view already read looks exactly like an N+1 inside the view. Nothing is
    excluded by adding ``request_finished`` — the body's queries still form a
    window of their own and are still checked, they are just no longer counted
    against the request's.

    Connects to both signals on construction, so it must be closed to
    disconnect. ``guarding()`` does that in a ``finally``.
    """

    def __init__(self):
        self.shapes: list[str] = []
        self.windows: list[list[str]] = []
        self._closed = False
        request_started.connect(self._bank_window)
        request_finished.connect(self._bank_window)
        with _ACTIVE_LOCK:
            _ACTIVE.append(self)

    def _bank_window(self, **kwargs) -> None:
        self.windows.append(self.shapes)
        self.shapes = []

    def bank_window(self) -> None:
        """Close the current window and open a fresh one. No-op once closed."""
        if self._closed:
            return
        self._bank_window()

    def close(self) -> None:
        """Disconnect the signals and bank the final window. Idempotent."""
        if self._closed:
            return
        request_started.disconnect(self._bank_window)
        request_finished.disconnect(self._bank_window)
        self._bank_window()
        self._closed = True
        with _ACTIVE_LOCK:
            if self in _ACTIVE:
                _ACTIVE.remove(self)

    def __call__(self, execute, sql, params, many, context):
        if is_select(sql):
            self.shapes.append(sql)
        return execute(sql, params, many, context)

    def repeated(self, threshold: int) -> dict[str, int]:
        """Shapes appearing more than ``threshold`` times in any one window."""
        offenders: dict[str, int] = {}
        for window in self.windows:
            for sql, count in Counter(window).items():
                if count > threshold:
                    offenders[sql] = max(offenders.get(sql, 0), count)
        return offenders


@contextmanager
def new_window():
    """Bracket a unit of work Django does not signal.

    Celery task bodies, management commands and service functions called
    directly from a test emit no ``request_started``, so without this the whole
    test body is one window and a loop over two of them reads as an N+1 of
    every shape they share.

    Banks on both entry and exit, exactly as the request signals do, so the work
    inside is counted on its own and is not charged to whatever ran before or
    after it. Safe to call with no guard active — it is then a no-op, which is
    what lets production code use it unconditionally if that reads better than
    confining it to tests.
    """
    _bank_all()
    try:
        yield
    finally:
        _bank_all()


def _bank_all() -> None:
    with _ACTIVE_LOCK:
        collectors = list(_ACTIVE)
    for collector in collectors:
        collector.bank_window()


def format_finding(test_id: str, offenders: dict[str, int], threshold: int | None = None) -> str:
    """Render one test's offending shapes as an indented block."""
    lines = []
    for sql, count in sorted(offenders.items()):
        prefix = f"  {count}x" if threshold is None else f"  {count}x (allowed {threshold})"
        lines.append(f"{prefix}  {sql}")
    return f"{test_id}\n" + "\n".join(lines)


def format_report(findings: list[tuple[str, dict[str, int]]]) -> str:
    """Render the whole run's findings, or a clean bill of health."""
    if not findings:
        return "query guard: no repeated query shapes detected"
    rule = "=" * 78
    blocks = [format_finding(test_id, offenders) for test_id, offenders in findings]
    return (
        f"\n{rule}\nquery guard: {len(findings)} test(s) with repeated query shapes\n"
        f"{rule}\n" + "\n".join(blocks) + f"\n{rule}\n"
        "Report-only mode: nothing failed. Fix these, or mark the deliberate ones with "
        "@allow_repeats(n), then set QUERY_GUARD_REPORT_ONLY = False to make this a gate."
    )


def emit_report() -> None:
    """Print any accumulated findings and clear them.

    Clearing is what makes this safe to call from more than one place:
    ``QueryGuardRunner`` reports at the end of its suite, and the ``atexit``
    registration below catches the case where the mixin was used on its own,
    with no runner to do it.
    """
    if not _FINDINGS:
        return
    # stderr, like the test runner's own summary and Django's teardown notices.
    # Sharing their stream is what keeps the report in the right place when the
    # two are merged, as they are in CI logs.
    print(format_report(_FINDINGS), file=sys.stderr, flush=True)
    _FINDINGS.clear()


atexit.register(emit_report)


def warm_content_types(test) -> None:
    """Populate the ``ContentType`` cache before a window opens.

    ``ContentType.objects.get_for_model`` caches on a process-global dict, so
    the *first* test in a process that touches two models inside one window pays
    two ``SELECT ... FROM django_content_type``. Those are one **shape** — the
    app label and model name are bound parameters, so the fingerprint cannot
    tell the two lookups apart — and at threshold 1 that reads as an N+1.

    The verdict would otherwise depend on which modules had already run, which
    is the real defect: the whole suite passes and one module in isolation
    fails, on identical code. A false positive that only appears in isolation
    teaches exactly the reflex this guard exists to prevent — reach for
    ``@allow_repeats(2)``, which then permanently raises that test's ceiling and
    hides a real repeat of any shape.

    Warming rather than excluding ``django_content_type`` from counting, which
    is the other available fix: in a served request the cache is warm after the
    first lookup, so a warm cache is what production *does*, and the guard
    should measure that. Excluding the table would blind the guard to code that
    clears the cache and then looks two models up.

    **Read-only by construction, which is why this does not use
    ``get_for_models``.** That is the obvious call and it *writes*: it ends in
    ``self.create(...)`` for any requested model with no row. That would quietly
    subvert ``TransactionTestCase.available_apps``, whose whole purpose is to
    leave the excluded apps' content types absent after a flush. Reading the
    table and seeding the cache cannot write, so the property holds by shape
    rather than by the row set happening to be complete.

    Runs *before* the collector's wrapper is attached, so it is never charged to
    the window it protects, and per window rather than once per process because
    ``TransactionTestCase`` flushes and re-emits ``post_migrate``, which clears
    this cache again.

    ``test`` is read only for its ``databases``. A ``SimpleTestCase`` declares
    an empty set and cannot query at all, so warming it would raise
    ``DatabaseOperationForbidden`` once per test and the ``except`` below would
    swallow every one. Skipping the case that cannot be warmed is what keeps
    that ``except`` meaning "something unexpected happened" rather than "this
    ran normally".
    """
    global _WARM_FAILURE_REPORTED
    # Checked rather than left to the `except`: a project not using contenttypes
    # is an ordinary configuration, not a problem to report once per process.
    if not apps.is_installed("django.contrib.contenttypes"):
        return
    try:
        from django.contrib.contenttypes.models import ContentType

        manager = ContentType.objects
        if manager.db not in getattr(test, "databases", {"default"}):
            return
        for content_type in manager.all():
            manager._add_to_cache(manager.db, content_type)
    except Exception as exc:  # pragma: no cover - defensive
        # Loud, because a silent no-op here restores the false positive this
        # prevents. Once, because it would otherwise be once per test.
        if not _WARM_FAILURE_REPORTED:
            _WARM_FAILURE_REPORTED = True
            print(
                f"query guard: could not warm the ContentType cache "
                f"({type(exc).__name__}: {exc}). Tests touching two models in one "
                f"window may report a false N+1.",
                file=sys.stderr,
            )


@contextmanager
def guarding(test):
    """Collect ``test``'s queries, then check them, whatever the outcome.

    Re-entrant, because the two entry points compose: a ``QueryGuardMixin`` test
    running under ``QueryGuardRunner`` is wrapped twice, and every query — and
    every finding — would otherwise be recorded once per layer. Only the
    outermost context collects; inner ones are pass-throughs.
    """
    if getattr(test, "_query_guard_active", False):
        yield None
        return
    test._query_guard_active = True
    # Before the collector's execute_wrapper is attached, so the warm is never
    # charged to the window it protects. Constructing QueryCollector only
    # connects the signals; recording starts inside the ExitStack.
    warm_content_types(test)
    collector = QueryCollector()
    completed = False
    try:
        with ExitStack() as stack:
            # Every alias, not just `default`: a project with two databases
            # would otherwise be guarded on one of them.
            for connection in connections.all():
                stack.enter_context(connection.execute_wrapper(collector))
            yield collector
        completed = True
    finally:
        collector.close()
        test._query_guard_active = False
        check(test, collector, completed=completed)


def threshold_for(test) -> int:
    """The repeat threshold for this test, honouring ``@allow_repeats``."""
    test_method = getattr(test, getattr(test, "_testMethodName", ""), None)
    return getattr(test_method, "_allow_repeats", default_threshold())


def check(test, collector: QueryCollector, *, completed: bool = True) -> None:
    """Record or raise, depending on mode. Called once per test.

    ``completed`` is False when the test body raised — a failure, an error, or
    an in-body ``self.skipTest()``. Such a run measured only the queries made
    before it stopped, which is *fewer* than the test earns, so adding a second
    failure on top of the real one buries the cause. Decorator skips
    (``@skipIf``, ``@unittest.skip``) never reach here at all, because
    ``_callTestMethod`` is not called for them.
    """
    threshold = threshold_for(test)
    offenders = collector.repeated(threshold)
    if not offenders or not completed:
        return
    if report_only():
        _FINDINGS.append((test.id(), offenders))
        return
    fail_with(test, test.id(), threshold, offenders)


def fail_with(test, test_id: str, threshold: int, offenders: dict[str, int]) -> None:
    """Fail ``test``, naming the allowance each shape broke and what to do."""
    test.fail(
        "repeated query shape (likely N+1):\n"
        + format_finding(test_id, offenders, threshold)
        + "\n\nIf the repetition is deliberate, decorate the test with "
        "@allow_repeats(n). If it is a real N+1, fix it — select_related or "
        "prefetch_related is usually the answer. If the repeats are separate "
        "units of work, bracket each one with new_window()."
    )


def install():
    """Wrap ``unittest.TestCase._callTestMethod`` so every test is guarded.

    Returns the callable it replaced, so a caller can put it back. That return
    value is the only way back: the wrapper closes over what it replaced, so
    re-reading the attribute after the fact hands you the wrapper.

    ``_callTestMethod`` is the narrowest available hook and the only one that
    brackets the test body alone — ``startTest`` on the result object fires
    before ``setUp``, which would charge fixture reads to the test.
    """
    original = unittest.TestCase._callTestMethod

    def guarded(test, method):
        with guarding(test):
            original(test, method)

    unittest.TestCase._callTestMethod = guarded
    return original


def _init_guarded_worker(counter, *args, **kwargs):
    """``--parallel`` worker init: run Django's, then install the guard.

    Necessary, not belt-and-braces. Workers are separate processes, and on macOS
    ``multiprocessing`` starts them with ``spawn``, which inherits no
    monkeypatch from the parent — so a ``--parallel`` run would guard
    **nothing** while reporting the same pass count as a serial one. That is
    worse than no guard: a green that reads as a checked green.

    A module-level function on purpose: it is passed to a ``Pool`` initializer
    and has to survive pickling under spawn.
    """
    from django.test.runner import _init_worker

    _init_worker(counter, *args, **kwargs)
    install()


class GuardedParallelTestSuite(ParallelTestSuite):
    init_worker = _init_guarded_worker


class QueryGuardRunner(DiscoverRunner):
    """Check every test in the suite without editing any of them.

    Blanket coverage is the point: a detector you have to opt into only ever
    finds N+1s in the tests somebody remembered to annotate.
    """

    parallel_test_suite = GuardedParallelTestSuite

    def run_suite(self, suite, **kwargs):
        if getattr(self, "parallel", 0) > 1:
            self._require_parallel_support()
        original = install()
        try:
            return super().run_suite(suite, **kwargs)
        finally:
            unittest.TestCase._callTestMethod = original
            emit_report()

    @staticmethod
    def _require_parallel_support() -> None:
        """Refuse ``--parallel`` if the private worker hook has moved.

        ``django.test.runner._init_worker`` is private, and wrapping it is the
        only way to install the guard inside a ``spawn``-started worker. Checked
        here rather than at import, and raised rather than warned, because the
        failure it prevents is a run that guards nothing and still prints a full
        pass count. A library that cannot keep its promise must say so where the
        promise is made, not load fine and quietly stop checking.
        """
        try:
            from django.test.runner import _init_worker  # noqa: F401
        except ImportError as exc:  # pragma: no cover - only on an unexpected Django
            raise ImproperlyConfigured(
                "query guard: django.test.runner._init_worker is gone, so the guard "
                "cannot be installed in --parallel workers and the run would check "
                "nothing. Run serially (--parallel=1), or upgrade abi-django-utils."
            ) from exc


class QueryGuardMixin:
    """Mix into a ``TestCase`` to check just that class for repeated shapes.

    Use this to guard one area without turning the runner on for everything.
    Mode and threshold come from the settings described in the module docstring.

    **A guarded class cannot contain its own positive control.** A nested
    ``TestCase`` run from inside a guarded test has its queries charged to the
    outer collector — that is the re-entrancy rule above doing its job — so a
    deliberately-planted N+1 fails the wrong test. Put the control in an
    unguarded sibling that subclasses the guarded class.
    """

    def _callTestMethod(self, method):
        with guarding(self):
            super()._callTestMethod(method)
