"""Tests for the blanket N+1 query guard.

Most of these run an **inner** test case under the guard and assert on the
result object, because the thing under test is "does a test fail" rather than
"does a function return". Inner classes are defined inside the test method on
purpose: it keeps the planted N+1 next to the assertion about it, and pytest
does not collect them.

The inner classes are ``SimpleTestCase`` with ``databases`` opened rather than
``TestCase``, so they do not open a transaction of their own inside the outer
test's. Their writes are rolled back by the outer ``TestCase`` regardless.
"""

import asyncio
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import request_finished, request_started
from django.db import connections
from django.test import AsyncClient, Client, SimpleTestCase, TestCase, override_settings

from abi_django_utils import queryguard
from abi_django_utils.queryguard import (
    REGENERATE_COMMAND,
    GuardedParallelTestSuite,
    LegacyBaselineFormat,
    QueryCollector,
    QueryGuardMixin,
    QueryGuardRunner,
    UnreadableBaselineEntry,
    allow_repeats,
    baselinable,
    baseline_path,
    carryable_baseline,
    check,
    default_threshold,
    emit_report,
    expect_repeats,
    expected_repeats_for,
    format_finding,
    format_report,
    guarding,
    install,
    is_select,
    load_baseline,
    merge_baseline,
    new_window,
    read_baseline_json,
    report_only,
    stale_entries,
    threshold_for,
    unbaselined_shapes,
    updating_baseline,
    warm_content_types,
    write_baseline,
)
from tests.queryguard_app.models import Author, Book

SELECT_BOOKS = 'SELECT "book"."id" FROM "book"'
SELECT_AUTHOR = 'SELECT "author"."id" FROM "author" WHERE "id" = %s'


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Keep each test's findings and active collectors out of its neighbours'.

    ``_FINDINGS`` is module-global by design — the report is per *run*, not per
    test — so without this a leftover finding from one test shows up in the next
    one's assertion, and the suite prints a spurious report at exit.
    """
    queryguard._FINDINGS.clear()
    queryguard._OBSERVED.clear()
    queryguard._OBSERVATIONS = 0
    queryguard._RUN_ACTIVE = False
    queryguard._RUN_BASELINE_PATH = None
    os.environ.pop("QUERY_GUARD_UPDATE_BASELINE", None)
    for leaked in list(queryguard._ACTIVE):
        leaked.close()
    yield
    queryguard._FINDINGS.clear()
    queryguard._OBSERVED.clear()
    queryguard._OBSERVATIONS = 0
    queryguard._RUN_ACTIVE = False
    queryguard._RUN_BASELINE_PATH = None
    os.environ.pop("QUERY_GUARD_UPDATE_BASELINE", None)
    # close(), not clear(): a collector left in the registry still holds live
    # `request_started`/`request_finished` receivers, and clearing the list
    # would hide that leak instead of undoing it.
    for leaked in list(queryguard._ACTIVE):
        leaked.close()


def collect(*statements, boundaries=()):
    """Feed ``statements`` to a collector, banking a window at each boundary index."""
    collector = QueryCollector()
    try:
        for index, sql in enumerate(statements):
            if index in boundaries:
                collector.bank_window()
            collector(lambda sql, params, many, context: None, sql, (), False, {})
    finally:
        collector.close()
    return collector


def run_inner(cls, method_name="runTest"):
    """Run one inner test method and hand back unittest's result object.

    ``testsRun`` is asserted here rather than in each caller: every
    ``wasSuccessful()`` assertion below would otherwise also pass on an inner
    test that never ran.
    """
    result = unittest.TestResult()
    unittest.TestSuite([cls(method_name)]).run(result)
    assert result.testsRun == 1, f"inner test did not run: {result.testsRun}"
    return result


def only_failure(result):
    """The single failure message on ``result``, asserting there is exactly one."""
    assert result.errors == [], result.errors
    assert len(result.failures) == 1, result.failures
    return result.failures[0][1]


def seed_books(count=3):
    author = Author.objects.create(name="Ursula")
    for index in range(count):
        Book.objects.create(title=f"book-{index}", author=author)
    return author


def touch_authors():
    """A real N+1: one query for the list, then one per row."""
    return [book.author.name for book in Book.objects.all()]


def touch_authors_eagerly():
    return [book.author.name for book in Book.objects.select_related("author")]


class IsSelectTests(SimpleTestCase):
    def test_plain_select(self):
        assert is_select("SELECT 1")

    def test_leading_whitespace_and_newline(self):
        assert is_select("\n   SELECT 1")

    def test_lowercase(self):
        assert is_select("select 1")

    def test_insert_is_not_a_read(self):
        assert not is_select('INSERT INTO "book" ("title") VALUES (%s)')

    def test_update_is_not_a_read(self):
        assert not is_select('UPDATE "book" SET "title" = %s')

    def test_a_select_inside_an_insert_does_not_count(self):
        # The guard counts reads. An INSERT ... SELECT is a write whose shape
        # repeats once per bulk load, and counting it reads as an N+1.
        assert not is_select('INSERT INTO "book" SELECT * FROM "draft"')


class CollectorCountingTests(SimpleTestCase):
    def test_one_occurrence_is_not_a_finding(self):
        assert collect(SELECT_BOOKS).repeated(1) == {}

    def test_two_occurrences_of_one_shape_is_a_finding(self):
        assert collect(SELECT_AUTHOR, SELECT_AUTHOR).repeated(1) == {SELECT_AUTHOR: 2}

    def test_threshold_is_exclusive(self):
        # "more than threshold", so threshold 2 tolerates exactly 2.
        assert collect(SELECT_AUTHOR, SELECT_AUTHOR).repeated(2) == {}
        assert collect(SELECT_AUTHOR, SELECT_AUTHOR, SELECT_AUTHOR).repeated(2) == {SELECT_AUTHOR: 3}

    def test_distinct_shapes_are_not_each_others_repeats(self):
        assert collect(SELECT_BOOKS, SELECT_AUTHOR).repeated(1) == {}

    def test_writes_are_not_counted(self):
        insert = 'INSERT INTO "book" ("title") VALUES (%s)'
        assert collect(insert, insert, insert).repeated(1) == {}

    def test_the_worst_window_is_the_one_reported(self):
        collector = collect(
            SELECT_AUTHOR,
            SELECT_AUTHOR,
            SELECT_AUTHOR,
            SELECT_AUTHOR,
            boundaries=(2,),
        )
        # Two windows of two. Not four.
        assert collector.repeated(1) == {SELECT_AUTHOR: 2}

    def test_wrapper_passes_the_statement_through(self):
        seen = []

        def execute(sql, params, many, context):
            seen.append((sql, params, many))
            return "result"

        collector = QueryCollector()
        try:
            assert collector(execute, SELECT_BOOKS, (7,), False, {}) == "result"
        finally:
            collector.close()
        assert seen == [(SELECT_BOOKS, (7,), False)]


class WindowBoundaryTests(TestCase):
    def test_the_same_shape_once_per_window_is_clean(self):
        assert collect(SELECT_AUTHOR, SELECT_AUTHOR, boundaries=(1,)).repeated(1) == {}

    def test_request_started_banks_a_window(self):
        collector = QueryCollector()
        try:
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
            request_started.send(sender=self.__class__)
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {}

    def test_request_finished_banks_a_window(self):
        # The regression this signal exists for: work done AFTER the last
        # request must not be charged to that request. Banking on
        # request_started alone leaves the request's window open until the next
        # request opens, which may never happen.
        collector = QueryCollector()
        try:
            request_started.send(sender=self.__class__)
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
            request_finished.send(sender=self.__class__)
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {}

    def test_close_banks_the_final_window(self):
        # Queries made with no request at all are still checked.
        collector = QueryCollector()
        collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        assert collector.windows == []
        collector.close()
        assert collector.repeated(1) == {SELECT_AUTHOR: 2}

    def test_close_is_idempotent_and_does_not_double_bank(self):
        collector = QueryCollector()
        collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        collector.close()
        banked = len(collector.windows)
        collector.close()
        assert len(collector.windows) == banked

    def test_close_disconnects_the_signals(self):
        collector = QueryCollector()
        collector.close()
        banked = len(collector.windows)
        request_started.send(sender=self.__class__)
        request_finished.send(sender=self.__class__)
        assert len(collector.windows) == banked

    def test_bank_window_is_inert_after_close(self):
        collector = QueryCollector()
        collector.close()
        banked = len(collector.windows)
        collector.bank_window()
        assert len(collector.windows) == banked


class NewWindowTests(SimpleTestCase):
    """The answer to "Celery tasks and management commands are one window each"."""

    def test_two_bracketed_units_of_work_do_not_repeat_each_other(self):
        collector = QueryCollector()
        try:
            for _ in range(2):
                with new_window():
                    collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {}

    def test_without_the_bracket_the_same_two_units_are_a_finding(self):
        # The control. Without this, the test above passes for any reason at all
        # — including new_window() being a no-op and the loop only running once.
        collector = QueryCollector()
        try:
            for _ in range(2):
                collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {SELECT_AUTHOR: 2}

    def test_work_before_the_block_is_not_charged_to_it(self):
        # The entry bank. A mutation round found this untested: a loop of
        # bracketed units passes on the EXIT bank alone, so "two tasks do not
        # repeat each other" says nothing about whatever ran before the first.
        collector = QueryCollector()
        try:
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
            with new_window():
                collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {}

    def test_it_banks_on_exit_as_well_as_entry(self):
        # Work after the block is not charged to the block.
        collector = QueryCollector()
        try:
            with new_window():
                collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {}

    def test_it_banks_on_exit_even_when_the_body_raises(self):
        collector = QueryCollector()
        try:
            with pytest.raises(RuntimeError):
                with new_window():
                    collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
                    raise RuntimeError("task blew up")
            collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
        finally:
            collector.close()
        assert collector.repeated(1) == {}

    def test_it_is_a_no_op_with_no_guard_active(self):
        # So production code can call it unconditionally.
        with new_window():
            pass

    def test_a_closed_collector_is_not_banked_into(self):
        collector = QueryCollector()
        collector.close()
        banked = len(collector.windows)
        with new_window():
            pass
        assert len(collector.windows) == banked


class ThresholdTests(SimpleTestCase):
    def test_default_is_one(self):
        assert default_threshold() == 1

    @override_settings(QUERY_GUARD_MAX_REPEATS=4)
    def test_setting_is_read_at_call_time(self):
        assert default_threshold() == 4

    def test_allow_repeats_marks_the_method(self):
        @allow_repeats(7)
        def method():
            pass

        assert method._allow_repeats == 7

    def test_threshold_for_honours_the_decorator(self):
        class Inner(SimpleTestCase):
            @allow_repeats(9)
            def runTest(self):
                pass

        assert threshold_for(Inner("runTest")) == 9

    def test_threshold_for_falls_back_to_the_setting(self):
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        assert threshold_for(Inner("runTest")) == default_threshold()


class ReportOnlyTests(SimpleTestCase):
    def test_default_is_report_only(self):
        # A library that turns an adopter's suite red on install gets uninstalled.
        assert report_only() is True

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_setting_makes_it_a_gate(self):
        assert report_only() is False

    @override_settings(QUERY_GUARD_REPORT_ONLY=True)
    def test_a_finding_is_collected_and_the_test_passes(self):
        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                collector = queryguard._ACTIVE[0]
                collector(lambda *a: None, SELECT_AUTHOR, (), False, {})
                collector(lambda *a: None, SELECT_AUTHOR, (), False, {})

        result = run_inner(Inner)
        assert result.wasSuccessful()
        assert [test_id for test_id, _ in queryguard._FINDINGS] == [Inner("runTest").id()]


class EmitReportTests(SimpleTestCase):
    def test_nothing_is_printed_when_there_are_no_findings(self, capsys=None):
        emit_report()  # must not raise

    def test_the_report_names_the_test_and_clears(self, capsys=None):
        queryguard._FINDINGS.append(("app.Tests.test_thing", {SELECT_AUTHOR: 3}))
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            emit_report()
        printed = stderr.getvalue()
        assert "app.Tests.test_thing" in printed
        assert "3x" in printed
        assert queryguard._FINDINGS == []

    def test_a_second_call_prints_nothing(self):
        queryguard._FINDINGS.append(("app.Tests.test_thing", {SELECT_AUTHOR: 3}))
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            emit_report()
            first = stderr.getvalue()
            emit_report()
        assert stderr.getvalue() == first


class FormattingTests(SimpleTestCase):
    def test_a_finding_without_a_threshold_shows_only_the_count(self):
        rendered = format_finding("a.B.test_c", {SELECT_AUTHOR: 3})
        assert "3x" in rendered
        assert "allowed" not in rendered

    def test_a_finding_with_an_allowance_names_it(self):
        rendered = format_finding("a.B.test_c", {SELECT_AUTHOR: 3}, {SELECT_AUTHOR: 1})
        assert "3x (allowed 1)" in rendered

    def test_each_shape_is_rendered_against_its_own_allowance(self):
        # The reason this takes a mapping rather than one number. With a baseline
        # the shapes in a single finding are judged against different allowances,
        # so one number in the header is wrong for at least one line and a reader
        # seeing `2x` under `allowed 5` concludes the guard is broken.
        rendered = format_finding(
            "a.B.test_c",
            {SELECT_AUTHOR: 6, SELECT_BOOKS: 2},
            {SELECT_AUTHOR: 5, SELECT_BOOKS: 1},
        )
        assert "6x (allowed 5)" in rendered
        assert "2x (allowed 1)" in rendered

    def test_a_shape_missing_from_the_allowances_shows_only_its_count(self):
        rendered = format_finding("a.B.test_c", {SELECT_AUTHOR: 3}, {SELECT_BOOKS: 1})
        assert "3x  " in rendered
        assert "allowed" not in rendered

    def test_the_finding_names_the_test_and_the_sql(self):
        rendered = format_finding("a.B.test_c", {SELECT_AUTHOR: 3})
        assert rendered.startswith("a.B.test_c\n")
        assert SELECT_AUTHOR in rendered

    def test_shapes_are_rendered_in_a_stable_order(self):
        rendered = format_finding("a.B.test_c", {SELECT_BOOKS: 2, SELECT_AUTHOR: 3})
        assert rendered.index(SELECT_AUTHOR) < rendered.index(SELECT_BOOKS)

    def test_an_empty_run_reports_a_clean_bill_of_health(self):
        assert format_report([]) == "query guard: no repeated query shapes detected"

    def test_the_report_counts_the_tests_and_says_nothing_failed(self):
        rendered = format_report([("a.B.test_c", {SELECT_AUTHOR: 3}), ("a.B.test_d", {SELECT_BOOKS: 2})])
        assert "2 test(s)" in rendered
        assert "a.B.test_c" in rendered and "a.B.test_d" in rendered
        # Report-only is not a pass, and the report has to say so.
        assert "QUERY_GUARD_REPORT_ONLY = False" in rendered


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class EnforcingTests(TestCase):
    def test_a_planted_nplusone_fails_the_test(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()

        message = only_failure(run_inner(Inner))
        assert "repeated query shape (likely N+1)" in message
        assert "queryguard_app_author" in message

    def test_the_failure_prescribes_the_three_available_fixes(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()

        message = only_failure(run_inner(Inner))
        assert "@allow_repeats" in message
        assert "select_related" in message
        assert "new_window()" in message

    def test_eager_loading_passes(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors_eagerly()

        assert run_inner(Inner).wasSuccessful()

    def test_allow_repeats_excuses_the_decorated_test(self):
        seed_books(3)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @allow_repeats(3)
            def runTest(self):
                touch_authors()

        assert run_inner(Inner).wasSuccessful()

    def test_allow_repeats_still_fails_one_repeat_above_its_number(self):
        # The decorator raises the ceiling; it does not remove it.
        seed_books(4)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @allow_repeats(3)
            def runTest(self):
                touch_authors()

        assert "repeated query shape" in only_failure(run_inner(Inner))

    def test_a_failing_body_is_not_given_a_second_failure(self):
        # The body stopped early, so it made FEWER queries than it earns.
        # Reporting an N+1 on top of the real failure buries the cause.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()
                self.fail("the real failure")

        message = only_failure(run_inner(Inner))
        assert "the real failure" in message
        assert "repeated query shape" not in message

    def test_an_in_body_skip_is_not_a_finding(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()
                self.skipTest("not today")

        result = run_inner(Inner)
        assert result.wasSuccessful()
        assert len(result.skipped) == 1

    def test_check_is_a_no_op_on_an_incomplete_run(self):
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        inner = Inner("runTest")
        collector = collect(SELECT_AUTHOR, SELECT_AUTHOR)
        check(inner, collector, completed=False)
        assert queryguard._FINDINGS == []


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class RequestWindowIntegrationTests(TestCase):
    """End to end, through the real test client and a real view."""

    def test_an_nplusone_in_a_view_is_caught(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                Client().get("/nplusone/")

        assert "queryguard_app_author" in only_failure(run_inner(Inner))

    def test_an_eager_view_passes(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                Client().get("/eager/")

        assert run_inner(Inner).wasSuccessful()

    def test_two_requests_to_a_clean_view_are_not_each_others_repeats(self):
        # This is the whole reason windows exist. Without per-request scoping a
        # test that hits the same correct page twice reports an N+1.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                Client().get("/eager/")
                Client().get("/eager/")

        assert run_inner(Inner).wasSuccessful()

    def test_work_after_the_last_request_is_not_charged_to_it(self):
        # request_finished closes the window. Without it, the body's re-read of
        # a row the view already read lands inside the view's window.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                Client().get("/eager/")
                touch_authors_eagerly()

        assert run_inner(Inner).wasSuccessful()


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class AsyncCoverageTests(TestCase):
    """Pins where the guard can and cannot see, in both directions.

    The extraction ticket's gap list called async "untested". Measuring it found
    the gap mis-stated twice over. Async is not the axis: ``sync_to_async``'s
    default ``thread_sensitive=True`` means "the thread the outer *synchronous*
    caller is on", so what decides coverage is whether the event loop was entered
    through ``async_to_sync`` — and when it was not, thread-sensitive work goes to
    a shared pool worker whose connection the guard never wrapped.

    Every "not covered" test here has a "covered" control beside it, because an
    assertion that nothing was recorded passes just as well on a collector that
    records nothing at all.
    """

    def test_an_nplusone_in_an_async_view_is_caught(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                async_to_sync(AsyncClient().get)("/async-nplusone/")

        assert "queryguard_app_author" in only_failure(run_inner(Inner))

    def test_an_eager_async_view_passes(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                async_to_sync(AsyncClient().get)("/async-eager/")

        assert run_inner(Inner).wasSuccessful()

    def test_djangos_async_orm_is_covered(self):
        seed_books()

        async def repeat_a_shape():
            async for book in Book.objects.all():
                await Author.objects.aget(pk=book.author_id)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                async_to_sync(repeat_a_shape)()

        assert "queryguard_app_author" in only_failure(run_inner(Inner))

    def test_an_async_def_test_method_is_covered(self):
        # Django wraps a coroutine test in async_to_sync before calling it
        # (SimpleTestCase._setup_and_call), which is what puts it on the main
        # thread and therefore inside the guard.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            async def runTest(self):
                await sync_to_async(touch_authors)()

        assert "queryguard_app_author" in only_failure(run_inner(Inner))

    def test_allow_repeats_works_on_an_async_test_method(self):
        # Django replaces the *instance* attribute with an AsyncToSync wrapper
        # that carries no _allow_repeats, so reading only the instance attribute
        # ignored the decorator and then told the author to add it.
        seed_books(3)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @allow_repeats(3)
            async def runTest(self):
                await sync_to_async(touch_authors)()

        assert run_inner(Inner).wasSuccessful()

    def test_an_undecorated_async_test_method_still_fails(self):
        # The control for the test above: without the decorator the same body is
        # a finding, so the pass up there is the decorator working rather than the
        # guard having gone blind on coroutine tests.
        seed_books(3)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            async def runTest(self):
                await sync_to_async(touch_authors)()

        assert "repeated query shape" in only_failure(run_inner(Inner))

    def test_a_bare_asyncio_run_is_not_covered(self):
        # No outer synchronous caller, so thread-sensitive work falls through to
        # a shared single-worker pool rather than staying on this thread. The
        # off-thread query may succeed or raise depending on whether the
        # enclosing transaction has the table locked -- Django's test database
        # for ":memory:" is `cache=shared`, so it is the SAME database, not an
        # empty one. Either way the guard records nothing, and that is the claim.
        seed_books()

        async def repeat_a_shape():
            await sync_to_async(touch_authors)()
            await sync_to_async(touch_authors)()

        collector = QueryCollector()
        try:
            with ExitStack() as stack:
                for connection in connections.all():
                    stack.enter_context(connection.execute_wrapper(collector))
                with contextlib.suppress(Exception):
                    asyncio.run(repeat_a_shape())
        finally:
            collector.close()
        assert collector.windows == [[]], f"expected nothing recorded, got {collector.windows}"

    def test_the_same_work_entered_through_async_to_sync_is_covered(self):
        # The control. Identical coroutine, identical ORM calls; only the entry
        # point differs, which is the whole point of the table in the docstring.
        seed_books()

        async def repeat_a_shape():
            await sync_to_async(touch_authors)()
            await sync_to_async(touch_authors)()

        collector = QueryCollector()
        try:
            with ExitStack() as stack:
                for connection in connections.all():
                    stack.enter_context(connection.execute_wrapper(collector))
                async_to_sync(repeat_a_shape)()
        finally:
            collector.close()
        assert collector.repeated(1), "async_to_sync keeps the work on this thread and must be seen"

    def test_isolated_asyncio_test_case_is_not_hooked_at_all(self):
        # Worse than blind: `install()` patches `unittest.TestCase`, and
        # IsolatedAsyncioTestCase overrides `_callTestMethod` in its own class
        # dict, so the guard is never entered and nothing warns. Pinned
        # structurally -- if a future Python stops overriding it, this goes red
        # and the documented limitation needs revisiting rather than quietly
        # becoming wrong in the other direction.
        assert "_callTestMethod" in vars(unittest.IsolatedAsyncioTestCase)
        original = install()
        try:
            assert unittest.TestCase._callTestMethod is not original
            assert unittest.IsolatedAsyncioTestCase._callTestMethod is not (unittest.TestCase._callTestMethod)
        finally:
            unittest.TestCase._callTestMethod = original

    def test_off_main_thread_orm_work_is_invisible(self):
        # Documented limitation, pinned so it changes deliberately rather than by
        # accident. Django's `connections` is thread-local, so another thread
        # uses a connection the guard never wrapped.
        seed_books()
        collector = QueryCollector()
        try:

            def query():
                from django.db import connections as thread_connections

                try:
                    with contextlib.suppress(Exception):
                        touch_authors()
                finally:
                    # This thread opened a connection of its own -- which is the
                    # finding -- and nothing else will ever close it.
                    # `close_all()` cannot: DatabaseWrapper.close() refuses on an
                    # in-memory SQLite db on purpose, since closing it destroys
                    # the data. So close the DB-API handle underneath.
                    wrapper = thread_connections["default"]
                    if wrapper.connection is not None:
                        wrapper.connection.close()
                        wrapper.connection = None

            thread = threading.Thread(target=query)
            thread.start()
            thread.join()
        finally:
            collector.close()
        assert collector.windows == [[]], "the guard is not supposed to see another thread's queries"

    def test_a_thread_sensitive_executor_is_visible(self):
        # The control for the test above: same shape of call, default executor,
        # and the guard does see it. Without this the assertion above passes if
        # the collector is broken in any way at all.
        seed_books()
        collector = QueryCollector()
        try:
            with ExitStack() as stack:
                for connection in connections.all():
                    stack.enter_context(connection.execute_wrapper(collector))
                async_to_sync(sync_to_async(touch_authors))()
        finally:
            collector.close()
        assert collector.repeated(1), "the default executor stays on the main thread and must be seen"


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class ReentrancyTests(TestCase):
    def test_a_guarded_test_under_the_runners_patch_records_each_query_once(self):
        # Double-counting here would turn every two-row page into a finding.
        seed_books(2)
        original = install()
        try:

            class Inner(QueryGuardMixin, SimpleTestCase):
                databases = {"default"}

                def runTest(self):
                    touch_authors_eagerly()

            result = run_inner(Inner)
        finally:
            unittest.TestCase._callTestMethod = original
        assert result.wasSuccessful(), only_failure(result)

    def test_the_inner_context_yields_none(self):
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        inner = Inner("runTest")
        with guarding(inner) as outer:
            assert outer is not None
            with guarding(inner) as nested:
                assert nested is None

    def test_the_flag_is_cleared_so_a_later_test_is_still_guarded(self):
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        inner = Inner("runTest")
        with guarding(inner):
            pass
        assert inner._query_guard_active is False


class InstallTests(TestCase):
    def test_it_returns_the_callable_it_replaced(self):
        original = unittest.TestCase._callTestMethod
        returned = install()
        try:
            assert returned is original
            assert unittest.TestCase._callTestMethod is not original
        finally:
            unittest.TestCase._callTestMethod = original

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_it_guards_a_test_that_does_not_use_the_mixin(self):
        # Blanket coverage is the whole point: no annotation anywhere.
        original = install()
        try:

            class Inner(SimpleTestCase):
                databases = {"default"}

                def runTest(self):
                    Author.objects.create(name="a")
                    touch_authors()
                    touch_authors()

            result = run_inner(Inner)
        finally:
            unittest.TestCase._callTestMethod = original
        assert "repeated query shape" in only_failure(result)


class RunnerTests(SimpleTestCase):
    """Built with a real ``DiscoverRunner.__init__``.

    A hand-rolled ``__init__`` that only sets ``parallel`` looks like it isolates
    the test, and instead skips the half-dozen attributes
    ``get_test_runner_kwargs`` reads — so the probe dies inside Django rather
    than exercising ``run_suite`` at all.
    """

    def test_run_suite_installs_the_patch_and_puts_it_back(self):
        original = unittest.TestCase._callTestMethod
        seen = {}

        class Suite:
            def run(self, result, **kwargs):
                seen["patched"] = unittest.TestCase._callTestMethod is not original
                return result

        with unittest.mock.patch.object(
            unittest.TextTestRunner, "run", lambda self, suite: suite.run(unittest.TestResult())
        ):
            QueryGuardRunner().run_suite(Suite())

        assert seen["patched"] is True
        assert unittest.TestCase._callTestMethod is original

    def test_run_suite_puts_the_patch_back_even_when_the_suite_raises(self):
        original = unittest.TestCase._callTestMethod

        with unittest.mock.patch.object(unittest.TextTestRunner, "run", side_effect=RuntimeError("suite blew up")):
            with pytest.raises(RuntimeError):
                QueryGuardRunner().run_suite(unittest.TestSuite())

        assert unittest.TestCase._callTestMethod is original

    def test_run_suite_emits_the_report(self):
        queryguard._FINDINGS.append(("a.B.test_c", {SELECT_AUTHOR: 2}))
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            with unittest.mock.patch.object(unittest.TextTestRunner, "run", lambda self, suite: unittest.TestResult()):
                QueryGuardRunner().run_suite(unittest.TestSuite())
        assert "a.B.test_c" in stderr.getvalue()
        assert queryguard._FINDINGS == []

    def test_the_parallel_suite_installs_the_guard_in_its_workers(self):
        # A --parallel run inherits no monkeypatch under spawn, so without this
        # it would guard nothing and still print a full pass count.
        from django.test.runner import ParallelTestSuite

        assert GuardedParallelTestSuite.init_worker is not ParallelTestSuite.init_worker
        assert GuardedParallelTestSuite.init_worker.__name__ == "_init_guarded_worker"
        assert QueryGuardRunner.parallel_test_suite is GuardedParallelTestSuite

    def test_the_worker_initialiser_is_importable_by_name(self):
        # It is handed to a Pool initializer and has to survive pickling.
        import pickle

        assert pickle.loads(pickle.dumps(queryguard._init_guarded_worker)) is (queryguard._init_guarded_worker)

    def test_the_worker_initialiser_runs_djangos_init_then_installs(self):
        # Order is not cosmetic: _init_worker is what gives the worker its own
        # database connection, and install() has nothing to guard before it.
        original = unittest.TestCase._callTestMethod
        order = []

        def fake_init_worker(counter, *args, **kwargs):
            order.append(("django", unittest.TestCase._callTestMethod is original))

        with unittest.mock.patch("django.test.runner._init_worker", fake_init_worker):
            try:
                queryguard._init_guarded_worker(object())
                order.append(("installed", unittest.TestCase._callTestMethod is not original))
            finally:
                unittest.TestCase._callTestMethod = original

        assert order == [("django", True), ("installed", True)]

    def test_parallel_is_refused_when_djangos_private_hook_has_moved(self):
        import builtins

        real_import = builtins.__import__

        def no_init_worker(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "django.test.runner" and "_init_worker" in (fromlist or ()):
                raise ImportError("no _init_worker")
            return real_import(name, globals, locals, fromlist, level)

        with unittest.mock.patch.object(builtins, "__import__", no_init_worker):
            with pytest.raises(ImproperlyConfigured) as caught:
                QueryGuardRunner(parallel=4).run_suite(unittest.TestSuite())
        assert "--parallel" in str(caught.value)

    def test_the_refusal_happens_before_the_suite_runs(self):
        # Refusing after the fact would still have run an unguarded suite and
        # printed its pass count, which is the outcome the check exists to stop.
        ran = []

        import builtins

        real_import = builtins.__import__

        def no_init_worker(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "django.test.runner" and "_init_worker" in (fromlist or ()):
                raise ImportError("no _init_worker")
            return real_import(name, globals, locals, fromlist, level)

        with unittest.mock.patch.object(builtins, "__import__", no_init_worker):
            with unittest.mock.patch.object(unittest.TextTestRunner, "run", lambda self, suite: ran.append(1)):
                with pytest.raises(ImproperlyConfigured):
                    QueryGuardRunner(parallel=4).run_suite(unittest.TestSuite())
        assert ran == []

    def test_a_serial_run_does_not_consult_the_private_hook(self):
        # The check must not fire on the common path.
        called = []
        with unittest.mock.patch.object(QueryGuardRunner, "_require_parallel_support", lambda *a: called.append(1)):
            with unittest.mock.patch.object(unittest.TextTestRunner, "run", lambda self, suite: unittest.TestResult()):
                QueryGuardRunner(parallel=1).run_suite(unittest.TestSuite())
        assert called == []


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class RealRunnerTests(TestCase):
    """Runs a real suite through ``QueryGuardRunner``, not a mocked one.

    Every other runner test above mocks ``TextTestRunner.run``, which asserts the
    wiring and nothing about the outcome. This is the only test in the repo where
    the library's headline entry point actually causes a test to fail.
    """

    def _suite(self):
        books = Book.objects.count()
        assert books, "fixture missing"

        class Planted(SimpleTestCase):
            databases = {"default"}

            def test_nplusone(self):
                touch_authors()

        class Clean(SimpleTestCase):
            databases = {"default"}

            def test_eager(self):
                touch_authors_eagerly()

        return unittest.TestSuite([Planted("test_nplusone"), Clean("test_eager")])

    def test_the_runner_fails_the_planted_test_and_passes_the_clean_one(self):
        seed_books()
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            result = QueryGuardRunner().run_suite(self._suite())
        assert result.testsRun == 2
        assert result.errors == [], result.errors
        assert len(result.failures) == 1, result.failures
        failed_test, message = result.failures[0]
        assert failed_test._testMethodName == "test_nplusone"
        assert "repeated query shape" in message
        assert "queryguard_app_author" in message

    def test_the_runner_restores_the_patch_after_a_real_run(self):
        seed_books()
        original = unittest.TestCase._callTestMethod
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            QueryGuardRunner().run_suite(self._suite())
        assert unittest.TestCase._callTestMethod is original

    @override_settings(QUERY_GUARD_REPORT_ONLY=True)
    def test_a_clean_run_says_the_guard_was_active(self):
        # Otherwise a run with the guard installed and a run with TEST_RUNNER
        # misspelled print exactly the same thing, and since nothing is annotated
        # there is no other evidence it was on.
        seed_books()

        class Clean(SimpleTestCase):
            databases = {"default"}

            def test_eager(self):
                touch_authors_eagerly()

        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            QueryGuardRunner().run_suite(unittest.TestSuite([Clean("test_eager")]))
        assert "query guard: active, no repeated query shapes detected" in stderr.getvalue()

    @override_settings(QUERY_GUARD_REPORT_ONLY=True)
    def test_a_run_with_findings_prints_them_instead_of_the_clean_line(self):
        seed_books()
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            QueryGuardRunner().run_suite(self._suite())
        printed = stderr.getvalue()
        assert "repeated query shapes" in printed
        assert "no repeated query shapes detected" not in printed

    def test_enforcing_mode_prints_no_clean_line(self):
        # In enforcing mode a green suite IS the evidence, and an extra line on
        # every CI run is noise.
        seed_books()

        class Clean(SimpleTestCase):
            databases = {"default"}

            def test_eager(self):
                touch_authors_eagerly()

        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            QueryGuardRunner().run_suite(unittest.TestSuite([Clean("test_eager")]))
        assert "query guard:" not in stderr.getvalue()

    def test_parallel_is_refused_at_construction_not_at_run_time(self):
        # Django reaches run_suite only after setup_databases has cloned one test
        # database per worker, so a refusal there is correct and wasteful.
        import builtins

        real_import = builtins.__import__

        def no_init_worker(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "django.test.runner" and "_init_worker" in (fromlist or ()):
                raise ImportError("no _init_worker")
            return real_import(name, globals, locals, fromlist, level)

        with unittest.mock.patch.object(builtins, "__import__", no_init_worker):
            with pytest.raises(ImproperlyConfigured):
                QueryGuardRunner(parallel=4)


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class MultipleDatabaseTests(TestCase):
    """The gap the extraction ticket declares closed, actually exercised.

    ``guarding()`` enters ``execute_wrapper`` for every alias in
    ``connections.all()``. With one alias configured that loop body runs exactly
    once in the whole suite, and 100% line coverage cannot tell that apart from
    multi-database working.
    """

    databases = {"default", "secondary"}

    def test_an_nplusone_on_a_secondary_alias_is_caught(self):
        author = Author.objects.using("secondary").create(name="Ursula")
        for index in range(3):
            Book.objects.using("secondary").create(title=f"b{index}", author=author)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default", "secondary"}

            def runTest(self):
                for book in Book.objects.using("secondary").all():
                    book.author.name

        assert "queryguard_app_author" in only_failure(run_inner(Inner))

    def test_a_clean_query_on_a_secondary_alias_passes(self):
        author = Author.objects.using("secondary").create(name="Ursula")
        for index in range(3):
            Book.objects.using("secondary").create(title=f"b{index}", author=author)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default", "secondary"}

            def runTest(self):
                for book in Book.objects.using("secondary").select_related("author"):
                    book.author.name

        assert run_inner(Inner).wasSuccessful()

    def test_every_configured_alias_is_wrapped(self):
        class Inner(SimpleTestCase):
            databases = {"default", "secondary"}

            def runTest(self):
                pass

        inner = Inner("runTest")
        with guarding(inner) as collector:
            for alias in ("default", "secondary"):
                assert collector in connections[alias].execute_wrappers, alias


class IncompleteRunTests(TestCase):
    """What a body that raised contributes, per mode."""

    @override_settings(QUERY_GUARD_REPORT_ONLY=True)
    def test_report_only_still_records_a_test_that_failed_for_another_reason(self):
        # The report is informational and cannot make a failing test worse.
        # Dropping it means the triage list the rollout is built on silently
        # under-reports, and the finding reappears only once the unrelated
        # failure is fixed.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()
                self.fail("something unrelated")

        result = run_inner(Inner)
        assert "something unrelated" in only_failure(result)
        assert [test_id for test_id, _ in queryguard._FINDINGS] == [Inner("runTest").id()]

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_enforcing_mode_does_not_pile_a_second_failure_on_the_first(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()
                self.fail("something unrelated")

        message = only_failure(run_inner(Inner))
        assert "something unrelated" in message
        assert "repeated query shape" not in message


class WarmContentTypesTests(TestCase):
    """The false positive that only appears when a module runs in isolation."""

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_a_cold_cache_does_not_make_two_model_lookups_an_nplusone(self):
        ContentType.objects.clear_cache()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                ContentType.objects.get_for_model(Author)
                ContentType.objects.get_for_model(Book)

        assert run_inner(Inner).wasSuccessful()

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_clearing_the_cache_inside_a_window_is_still_visible(self):
        # Warming is not the same as excluding the table. Code that clears the
        # cache and then looks two models up is a real repeat and must fail.
        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                ContentType.objects.clear_cache()
                ContentType.objects.get_for_model(Author)
                ContentType.objects.clear_cache()
                ContentType.objects.get_for_model(Book)

        assert "django_content_type" in only_failure(run_inner(Inner))

    def test_the_warm_is_not_charged_to_the_window_it_protects(self):
        # Position, not behaviour: warm_content_types runs before the collector's
        # execute_wrapper is attached. Moved after it, the warm's own
        # `SELECT ... FROM django_content_type` lands in the first window, which
        # is one occurrence away from being the false positive it exists to
        # prevent -- and no arm of warm_content_types itself can see where it is
        # called from.
        ContentType.objects.clear_cache()

        class Inner(SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                pass

        inner = Inner("runTest")
        with guarding(inner) as collector:
            pass
        recorded = [sql for window in collector.windows for sql in window]
        assert not any("django_content_type" in sql for sql in recorded), recorded

    def test_it_warms_the_cache(self):
        ContentType.objects.clear_cache()

        class Inner(SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                pass

        warm_content_types(Inner("runTest"))
        with self.assertNumQueries(0):
            ContentType.objects.get_for_model(Author)

    def test_a_test_that_declares_no_databases_is_skipped(self):
        # SimpleTestCase forbids queries, so warming it would raise once per
        # test and the except would swallow every one.
        ContentType.objects.clear_cache()

        class Inner(SimpleTestCase):
            databases = set()

            def runTest(self):
                pass

        with self.assertNumQueries(0):
            warm_content_types(Inner("runTest"))

    def test_it_does_not_write(self):
        # get_for_models() is the obvious call and it creates missing rows,
        # which would subvert TransactionTestCase.available_apps.
        ContentType.objects.clear_cache()
        before = set(ContentType.objects.values_list("app_label", "model"))
        ContentType.objects.filter(app_label="queryguard_app", model="book").delete()

        class Inner(SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                pass

        warm_content_types(Inner("runTest"))
        after = set(ContentType.objects.values_list("app_label", "model"))
        assert ("queryguard_app", "book") not in after
        assert after < before

    def test_a_forbidden_query_patch_is_not_reported_as_a_surprise(self):
        # A SimpleTestCase ancestor installs "no queries here" in setUpClass and
        # it stays in force while a hand-built inner case runs, so the inner
        # case's own `databases` does not re-enable the connection. That is
        # ordinary configuration: reporting it would make the loud `except` below
        # mean "this ran normally", which is exactly what it must not mean. This
        # fired on every run of this very suite before it was handled.
        from django.test.testcases import DatabaseOperationForbidden

        class Inner(SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                pass

        stderr = io.StringIO()
        with unittest.mock.patch.object(
            ContentType.objects.__class__,
            "all",
            side_effect=DatabaseOperationForbidden("nope"),
        ):
            with unittest.mock.patch("sys.stderr", stderr):
                warm_content_types(Inner("runTest"))
        assert stderr.getvalue() == ""

    def test_an_unexpected_failure_is_reported_once(self):
        # The other arm: anything that is NOT the forbidden-query patch must be
        # loud, because a silent no-op here restores the false positive the warm
        # exists to prevent.
        queryguard._WARM_FAILURE_REPORTED = False

        class Inner(SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                pass

        stderr = io.StringIO()
        try:
            with unittest.mock.patch.object(ContentType.objects.__class__, "all", side_effect=RuntimeError("boom")):
                with unittest.mock.patch("sys.stderr", stderr):
                    warm_content_types(Inner("runTest"))
                    first = stderr.getvalue()
                    warm_content_types(Inner("runTest"))
        finally:
            queryguard._WARM_FAILURE_REPORTED = False
        assert "could not warm the ContentType cache" in first
        assert "RuntimeError: boom" in first
        # Once per process, not once per test.
        assert stderr.getvalue() == first

    def test_the_forbidden_query_fallback_catches_nothing(self):
        # `except ()` catches nothing on purpose: if Django ever moves
        # DatabaseOperationForbidden, the warm must fall through to the loud
        # `except Exception` rather than silently swallowing every failure. The
        # obvious fallback -- `Exception` -- would do exactly the latter.
        import builtins

        real_import = builtins.__import__

        def no_forbidden(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "django.test.testcases" and "DatabaseOperationForbidden" in (fromlist or ()):
                raise ImportError("moved")
            return real_import(name, globals, locals, fromlist, level)

        with unittest.mock.patch.object(builtins, "__import__", no_forbidden):
            assert queryguard._forbidden_query_error() == ()

    def test_the_forbidden_query_sentinel_resolves_on_this_django(self):
        # The control: the fallback above is only correct BECAUSE the real path
        # works here. Without this, a permanently-broken import would read as a
        # passing test.
        from django.test.testcases import DatabaseOperationForbidden

        assert queryguard._forbidden_query_error() is DatabaseOperationForbidden
        assert queryguard._ForbiddenQuery is DatabaseOperationForbidden

    def test_a_databases_string_is_not_substring_matched(self):
        # `databases` is spelled "__all__" in source and is normally a frozenset
        # by the time setUpClass has run -- but `"default" not in "__all__"` is a
        # substring test that answers True, which would skip the warm.
        ContentType.objects.clear_cache()

        class Inner(SimpleTestCase):
            databases = "__all__"

            def runTest(self):
                pass

        warm_content_types(Inner("runTest"))
        with self.assertNumQueries(0):
            ContentType.objects.get_for_model(Author)

    def test_it_does_not_query_when_contenttypes_is_not_installed(self):
        # Asserting only that nothing was printed would pass with the guard
        # deleted: contenttypes IS installed in this suite, so the warm would
        # simply succeed. The guard's job is to not touch the table at all.
        ContentType.objects.clear_cache()

        class Inner(SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                pass

        stderr = io.StringIO()
        with unittest.mock.patch("django.apps.apps.is_installed", return_value=False):
            with unittest.mock.patch("sys.stderr", stderr):
                with self.assertNumQueries(0):
                    warm_content_types(Inner("runTest"))
        assert stderr.getvalue() == ""


# ---------------------------------------------------------------------------
# The baseline ratchet
# ---------------------------------------------------------------------------


@contextmanager
def baseline_file(contents=None, name="queryguard_baseline.json"):
    """A temp baseline the settings point at, yielded as a ``Path``.

    ``contents=None`` leaves the file absent, which is a distinct state from an
    empty one: a missing file is an empty baseline, while an empty *object* is a
    file that has been generated and records nothing.
    """
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / name
        if contents is not None:
            path.write_text(contents if isinstance(contents, str) else json.dumps(contents))
        with override_settings(QUERY_GUARD_BASELINE=str(path)):
            yield path


@contextmanager
def updating():
    """``QUERY_GUARD_UPDATE_BASELINE=1`` for the duration."""
    os.environ["QUERY_GUARD_UPDATE_BASELINE"] = "1"
    try:
        yield
    finally:
        os.environ.pop("QUERY_GUARD_UPDATE_BASELINE", None)


class BaselinePathTests(SimpleTestCase):
    """There is deliberately no default, and that is the library/app difference."""

    def test_unset_means_no_baseline(self):
        # Not a path beside the module: installed, that is inside the adopter's
        # site-packages, where a generated artifact is unreviewable and is deleted
        # by the next sync.
        assert baseline_path() is None

    @override_settings(QUERY_GUARD_BASELINE="/tmp/does-not-exist/baseline.json")
    def test_a_configured_string_becomes_a_path(self):
        assert baseline_path() == Path("/tmp/does-not-exist/baseline.json")

    @override_settings(QUERY_GUARD_BASELINE=Path("/tmp/x/baseline.json"))
    def test_a_configured_path_is_accepted_as_is(self):
        assert baseline_path() == Path("/tmp/x/baseline.json")

    @override_settings(QUERY_GUARD_BASELINE="")
    def test_an_empty_string_means_unset(self):
        # How a project spells "unset" when the value comes from the environment.
        # `Path("")` is `Path(".")`, which would fail much later as an
        # IsADirectoryError from a write rather than here as "not configured".
        assert baseline_path() is None

    def test_no_baseline_changes_nothing_about_enforcement(self):
        # The whole promise of having no default: an existing adopter's run is
        # byte-identical before and after this feature lands.
        assert load_baseline() == {}
        assert read_baseline_json() == {}
        assert carryable_baseline() == ({}, 0)


class UpdatingBaselineTests(SimpleTestCase):
    def test_absent_is_false(self):
        assert updating_baseline() is False

    def test_present_is_true(self):
        with updating():
            assert updating_baseline() is True

    def test_an_empty_value_is_false(self):
        # So `QUERY_GUARD_UPDATE_BASELINE= manage.py test` does not write.
        os.environ["QUERY_GUARD_UPDATE_BASELINE"] = ""
        try:
            assert updating_baseline() is False
        finally:
            os.environ.pop("QUERY_GUARD_UPDATE_BASELINE", None)


class BaselinableTests(SimpleTestCase):
    def test_a_discoverable_id_may_be_baselined(self):
        assert baselinable("tests.test_thing.SomeTests.test_it") is True

    def test_a_class_built_inside_a_test_method_may_not(self):
        # Baselining one silences the tests whose whole job is to prove the guard
        # still fires: they then fail rather than the guard going quietly blind.
        assert baselinable("tests.test_q.Outer.test_x.<locals>.Inner.runTest") is False

    def test_the_guards_own_inner_classes_are_excluded(self):
        # Measured against a real id rather than a hand-written one, because the
        # check is a substring test and the thing it has to match is whatever
        # Python actually puts in a nested class's qualname.
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        assert baselinable(Inner("runTest").id()) is False


class ReadBaselineJsonTests(SimpleTestCase):
    def test_a_missing_file_is_an_empty_baseline(self):
        with baseline_file():
            assert read_baseline_json() == {}

    def test_a_present_file_is_parsed(self):
        with baseline_file({"a.B.test_c": {SELECT_AUTHOR: 2}}):
            assert read_baseline_json() == {"a.B.test_c": {SELECT_AUTHOR: 2}}

    def test_invalid_json_names_the_file_and_the_remedy(self):
        with baseline_file("{not json") as path:
            with pytest.raises(RuntimeError) as caught:
                read_baseline_json()
        message = str(caught.value)
        assert str(path) in message
        assert REGENERATE_COMMAND in message

    def test_valid_json_of_the_wrong_shape_names_the_type(self):
        # A list reached `.items()` as a bare AttributeError, which names neither
        # the file nor the remedy.
        with baseline_file("[]") as path:
            with pytest.raises(RuntimeError) as caught:
                read_baseline_json()
        message = str(caught.value)
        assert "list" in message
        assert str(path) in message
        assert REGENERATE_COMMAND in message

    def test_whole_file_corruption_is_not_a_per_entry_refusal(self):
        # There are no entries to carry past, so this must not be catchable as
        # one -- `carryable_baseline` would otherwise swallow a corrupt file and
        # silently regenerate from nothing.
        with baseline_file("{not json"):
            with pytest.raises(RuntimeError) as caught:
                carryable_baseline()
        assert not isinstance(caught.value, UnreadableBaselineEntry)


class ParseEntryTests(SimpleTestCase):
    """Every refusal, and every one of them prescribes the same command."""

    def _refusal(self, entry):
        with baseline_file({"a.B.test_c": entry}):
            with pytest.raises(UnreadableBaselineEntry) as caught:
                load_baseline()
        return caught.value

    def test_a_bare_integer_is_refused_as_the_legacy_format(self):
        # Refused rather than dual-read: a scalar carries no record of which shape
        # earned it, so there is nothing to migrate it *from*, and guessing "every
        # shape" would carry the hole forward under a format that reads as fixed.
        error = self._refusal(2)
        assert isinstance(error, LegacyBaselineFormat)
        assert REGENERATE_COMMAND in str(error)

    def test_a_boolean_is_not_mistaken_for_the_legacy_format(self):
        # `isinstance(True, int)` is True in Python, so without the bool check a
        # hand-edited `true` would earn the migration message instead of the
        # hand-edit one.
        error = self._refusal(True)
        assert not isinstance(error, LegacyBaselineFormat)
        assert "mapping" in str(error)

    def test_a_list_entry_is_refused(self):
        assert "mapping" in str(self._refusal([SELECT_AUTHOR]))

    def test_a_string_entry_is_refused(self):
        assert "mapping" in str(self._refusal("two"))

    def test_a_non_integer_count_is_refused(self):
        assert "invalid count" in str(self._refusal({SELECT_AUTHOR: "lots"}))

    def test_a_boolean_count_is_refused(self):
        assert "invalid count" in str(self._refusal({SELECT_AUTHOR: True}))

    def test_a_zero_count_is_refused(self):
        # It excuses nothing (`max(declared, 0)` is `declared`) and can never be
        # reported stale, so it would sit in the file forever meaning nothing.
        assert "invalid count" in str(self._refusal({SELECT_AUTHOR: 0}))

    def test_a_negative_count_is_refused(self):
        assert "invalid count" in str(self._refusal({SELECT_AUTHOR: -5}))

    def test_a_valid_entry_parses(self):
        with baseline_file({"a.B.test_c": {SELECT_AUTHOR: 3}}):
            assert load_baseline() == {"a.B.test_c": {SELECT_AUTHOR: 3}}

    def test_every_refusal_names_the_file_and_the_command(self):
        # The breadth claim `carryable_baseline` rests on: each refusal promises
        # that one command fixes it, so each has to say what it is. A single
        # refusal missing it is a message naming a remedy nothing provides.
        for entry in (2, True, [], "two", {SELECT_AUTHOR: 0}, {SELECT_AUTHOR: "lots"}):
            with self.subTest(entry=entry):
                with baseline_file({"a.B.test_c": entry}) as path:
                    with pytest.raises(UnreadableBaselineEntry) as caught:
                        load_baseline()
                message = str(caught.value)
                assert REGENERATE_COMMAND in message, entry
                assert str(path) in message, entry


class CarryableBaselineTests(SimpleTestCase):
    """Forgiving on regeneration, strict on enforcement."""

    def test_a_good_entry_beside_a_bad_one_survives(self):
        # The case that matters most: one bad line is what a hand-resolved merge
        # conflict on a few hundred lines of generated JSON produces, and
        # discarding every valid entry over it turns a conflict into a disarmed
        # ratchet.
        with baseline_file({"good": {SELECT_AUTHOR: 2}, "bad": 3}):
            carried, unreadable = carryable_baseline()
        assert carried == {"good": {SELECT_AUTHOR: 2}}
        assert unreadable == 1

    def test_every_refusal_kind_is_carried_past_not_just_the_legacy_one(self):
        # Catching only LegacyBaselineFormat left the other refusals naming a
        # remedy that died on the same exception.
        entries = {
            "legacy": 2,
            "boolean": True,
            "list": [],
            "zero-count": {SELECT_AUTHOR: 0},
            "string-count": {SELECT_AUTHOR: "lots"},
            "good": {SELECT_BOOKS: 4},
        }
        with baseline_file(entries):
            carried, unreadable = carryable_baseline()
        assert carried == {"good": {SELECT_BOOKS: 4}}
        assert unreadable == 5

    def test_enforcement_still_refuses_what_regeneration_forgives(self):
        with baseline_file({"good": {SELECT_AUTHOR: 2}, "bad": 3}):
            assert carryable_baseline()[1] == 1
            with pytest.raises(UnreadableBaselineEntry):
                load_baseline()

    def test_a_bug_inside_the_parser_is_not_counted_as_an_unreadable_entry(self):
        # The one path whose job is to be forgiving must not silently absorb a
        # genuine defect in the parser as "one more bad entry".
        with baseline_file({"a": {SELECT_AUTHOR: 2}}):
            with unittest.mock.patch.object(queryguard, "_parse_entry", side_effect=ValueError("parser bug")):
                with pytest.raises(ValueError):
                    carryable_baseline()


class UnbaselinedShapesTests(SimpleTestCase):
    """Per shape, which is the difference between a ratchet and a hole."""

    def test_a_recorded_shape_is_excused_at_its_recorded_count(self):
        assert unbaselined_shapes({SELECT_AUTHOR: 4}, {SELECT_AUTHOR: 4}, 1) == {}

    def test_a_recorded_shape_that_got_worse_is_not(self):
        assert unbaselined_shapes({SELECT_AUTHOR: 5}, {SELECT_AUTHOR: 4}, 1) == {SELECT_AUTHOR: 5}

    def test_an_entry_excuses_only_the_shape_it_names(self):
        # A whole-test allowance is the hole: a brand-new 2x repeat of a
        # completely unrelated query lands inside it silently.
        offenders = {SELECT_AUTHOR: 4, SELECT_BOOKS: 2}
        assert unbaselined_shapes(offenders, {SELECT_AUTHOR: 4}, 1) == {SELECT_BOOKS: 2}

    def test_a_shape_the_entry_does_not_name_is_judged_as_strictly_as_with_no_entry(self):
        with_entry = unbaselined_shapes({SELECT_BOOKS: 2}, {SELECT_AUTHOR: 9}, 1)
        without_entry = unbaselined_shapes({SELECT_BOOKS: 2}, {}, 1)
        assert with_entry == without_entry == {SELECT_BOOKS: 2}

    def test_a_small_recorded_count_cannot_undercut_a_decorator(self):
        # max(declared, recorded): deleting an entry must only ever tighten.
        assert unbaselined_shapes({SELECT_AUTHOR: 4}, {SELECT_AUTHOR: 2}, 5) == {}


class WriteBaselineTests(SimpleTestCase):
    def test_entries_are_written_and_counted(self):
        with baseline_file() as path:
            written = write_baseline({"a.B.test_c": {SELECT_AUTHOR: 2}}, path)
        assert written == 1

    def test_a_clean_test_is_not_written(self):
        # An entry recording nothing excuses nothing, and would be noise in the
        # diff the file exists to be read as.
        with baseline_file() as path:
            assert write_baseline({"a.B.test_c": {}}, path) == 0
            assert json.loads(path.read_text()) == {}

    def test_an_unbaselinable_id_is_not_written(self):
        inner_id = "tests.t.Outer.test_x.<locals>.Inner.runTest"
        with baseline_file() as path:
            assert write_baseline({inner_id: {SELECT_AUTHOR: 2}}, path) == 0

    def test_the_returned_count_is_what_was_written_not_what_was_offered(self):
        # Reporting len(observed) overstates the file by every entry dropped here,
        # and the `wrote (N entries)` line is the number a reader checks.
        observed = {"kept": {SELECT_AUTHOR: 2}, "clean": {}, "x.<locals>.y": {SELECT_BOOKS: 3}}
        with baseline_file() as path:
            assert write_baseline(observed, path) == 1

    def test_the_file_is_sorted_indented_and_newline_terminated(self):
        # The file's whole value is that a diff of it is readable.
        observed = {"b.T.test_2": {SELECT_BOOKS: 2}, "a.T.test_1": {SELECT_AUTHOR: 3}}
        with baseline_file() as path:
            write_baseline(observed, path)
            raw = path.read_text()
        assert raw.endswith("\n")
        assert raw.index("a.T.test_1") < raw.index("b.T.test_2")
        assert "\n  " in raw

    def test_the_shapes_inside_an_entry_are_sorted_too(self):
        # Asserted on the file's text, not on a re-parse, because the property is
        # about the diff a reviewer reads. The keys are compared in their *encoded*
        # form: SQL contains double quotes, which JSON escapes, so looking for the
        # Python string raises `substring not found` rather than failing the order
        # claim -- a green-looking assertion that was never about ordering at all.
        with baseline_file() as path:
            write_baseline({"a.T.test_1": {SELECT_BOOKS: 2, SELECT_AUTHOR: 3}}, path)
            raw = path.read_text()
        assert raw.index(json.dumps(SELECT_AUTHOR)) < raw.index(json.dumps(SELECT_BOOKS))

    def test_a_round_trip_reads_back_what_was_written(self):
        # The ordering tests above all pass on a file whose *values* are wrong.
        with baseline_file() as path:
            write_baseline({"a.T.test_1": {SELECT_AUTHOR: 3}}, path)
            assert load_baseline() == {"a.T.test_1": {SELECT_AUTHOR: 3}}


class MergeBaselineTests(SimpleTestCase):
    def test_a_test_this_run_did_not_exercise_is_carried(self):
        # Deleting it reads in review exactly like the ratchet tightening, while
        # actually disarming every test the run did not reach.
        merged, carried = merge_baseline({"old": {SELECT_AUTHOR: 2}}, {"new": {SELECT_BOOKS: 3}})
        assert merged == {"new": {SELECT_BOOKS: 3}, "old": {SELECT_AUTHOR: 2}}
        assert carried == ["old"]

    def test_an_observed_test_that_now_repeats_nothing_is_dropped(self):
        # The one deletion the run has evidence for: this is the ratchet
        # tightening.
        merged, carried = merge_baseline({"fixed": {SELECT_AUTHOR: 2}}, {"fixed": {}})
        assert merged == {"fixed": {}}
        assert carried == []

    def test_carried_ids_are_sorted(self):
        existing = {"c": {SELECT_AUTHOR: 2}, "a": {SELECT_AUTHOR: 2}, "b": {SELECT_AUTHOR: 2}}
        assert merge_baseline(existing, {})[1] == ["a", "b", "c"]


class StaleEntriesTests(SimpleTestCase):
    def test_a_test_that_did_not_run_is_not_judged(self):
        # Otherwise a partial run reports the rest of the suite as stale.
        assert stale_entries({"a": {SELECT_AUTHOR: 3}}, {}) == []

    def test_an_entry_that_is_still_earned_is_not_stale(self):
        assert stale_entries({"a": {SELECT_AUTHOR: 3}}, {"a": {SELECT_AUTHOR: 3}}) == []

    def test_an_entry_whose_count_dropped_is_stale(self):
        assert stale_entries({"a": {SELECT_AUTHOR: 3}}, {"a": {SELECT_AUTHOR: 2}}) == ["a"]

    def test_a_shape_that_vanished_reads_as_zero(self):
        assert stale_entries({"a": {SELECT_AUTHOR: 3}}, {"a": {}}) == ["a"]

    def test_one_shape_dropping_is_caught_even_when_the_worst_count_is_unchanged(self):
        # Invisible to a whole-test check, which compares only the worst count.
        baseline = {"a": {SELECT_AUTHOR: 5, SELECT_BOOKS: 3}}
        observed = {"a": {SELECT_AUTHOR: 5, SELECT_BOOKS: 1}}
        assert stale_entries(baseline, observed) == ["a"]

    def test_a_worse_test_is_not_reported_as_stale(self):
        # Getting worse is `check`'s job to fail, not this report's to mention.
        assert stale_entries({"a": {SELECT_AUTHOR: 3}}, {"a": {SELECT_AUTHOR: 9}}) == []

    def test_stale_ids_are_sorted(self):
        baseline = {"c": {SELECT_AUTHOR: 3}, "a": {SELECT_AUTHOR: 3}}
        observed = {"c": {}, "a": {}}
        assert stale_entries(baseline, observed) == ["a", "c"]


class WorstCountTests(SimpleTestCase):
    """What ``@expect_repeats`` is asserted against."""

    def test_no_reads_is_zero(self):
        assert collect().worst_count() == 0

    def test_distinct_shapes_are_one(self):
        assert collect(SELECT_AUTHOR, SELECT_BOOKS).worst_count() == 1

    def test_a_repeat_is_counted(self):
        assert collect(SELECT_AUTHOR, SELECT_AUTHOR, SELECT_AUTHOR).worst_count() == 3

    def test_counts_are_not_summed_across_windows(self):
        # Two windows of two is not a four-deep N+1, and treating it as one would
        # make @expect_repeats(4) hold on code that repeats nothing per request.
        collector = collect(SELECT_AUTHOR, SELECT_AUTHOR, SELECT_AUTHOR, SELECT_AUTHOR, boundaries=(2,))
        assert collector.worst_count() == 2

    def test_the_worst_window_wins(self):
        collector = collect(SELECT_AUTHOR, SELECT_AUTHOR, SELECT_AUTHOR, boundaries=(1,))
        assert collector.worst_count() == 2

    def test_a_write_is_not_counted(self):
        assert collect("INSERT INTO book VALUES (1)", "INSERT INTO book VALUES (2)").worst_count() == 0


BASELINED_ID = "tests.test_queryguard.Baselined.test_nplusone"


def measure(body):
    """``{sql: worst count}`` for ``body``, without judging it.

    ``guarding()`` is the wrong tool for this: it calls ``check()`` on the way out,
    so in enforcing mode the probe fails before handing anything back. A bare
    collector records the same shapes and judges nothing.
    """
    collector = QueryCollector()
    try:
        with connections["default"].execute_wrapper(collector):
            body()
        collector.bank_window()
        return collector.repeated(0)
    finally:
        collector.close()


def one_shape(shapes, table):
    """The single recorded shape touching ``table``, and its count."""
    matching = {sql: count for sql, count in shapes.items() if table in sql}
    assert len(matching) == 1, f"expected one {table} shape, got {matching}"
    return next(iter(matching.items()))


def baselined_case(body, decorator=None):
    """A guarded case whose ``id()`` is one ``baselinable()`` accepts.

    Every other inner class in this file reports an id containing ``<locals>``,
    which can never carry a baseline entry — a baseline test written against one
    would pass by never consulting the file at all. Overriding ``id()`` is what
    production looks up the entry by, so this exercises the real key path; the
    ``<locals>`` exclusion is asserted separately below, against a **real** inner
    id rather than a synthetic one, so both directions are covered honestly.
    """

    class Baselined(QueryGuardMixin, SimpleTestCase):
        databases = {"default"}

        def id(self):
            return BASELINED_ID

        runTest = decorator(body) if decorator else body

    return Baselined


def plant_nplusone(self):
    touch_authors()


def plant_two_repeats(self):
    touch_authors()
    # A second, different shape repeating, so the finding has one baselined shape
    # and one that is not.
    list(Book.objects.filter(title="book-0"))
    list(Book.objects.filter(title="book-1"))


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class BaselinedEnforcementTests(TestCase):
    """``check`` against a baseline. The point of the whole mechanism."""

    def test_a_recorded_repeat_passes(self):
        seed_books()
        author_sql, count = one_shape(measure(touch_authors), "queryguard_app_author")
        with baseline_file({BASELINED_ID: {author_sql: count}}):
            result = run_inner(baselined_case(plant_nplusone))
        assert result.wasSuccessful(), result.failures

    def test_the_same_repeat_fails_with_no_entry(self):
        # The control for the test above: without it, "passes" could mean the
        # planted N+1 was never detected in the first place.
        seed_books()
        with baseline_file():
            result = run_inner(baselined_case(plant_nplusone))
        assert "repeated query shape" in only_failure(result)

    def test_a_recorded_repeat_that_got_worse_fails(self):
        seed_books()
        author_sql, count = one_shape(measure(touch_authors), "queryguard_app_author")
        assert count > 1, "fixture does not produce a repeat"
        with baseline_file({BASELINED_ID: {author_sql: count - 1}}):
            result = run_inner(baselined_case(plant_nplusone))
        message = only_failure(result)
        assert "repeated query shape" in message
        assert f"(allowed {count - 1})" in message

    def test_an_unrelated_new_shape_is_not_excused_by_the_entry(self):
        # The hole a whole-test allowance leaves: a brand-new repeat of a
        # completely different query lands inside it silently.
        seed_books()
        author_sql, count = one_shape(measure(touch_authors), "queryguard_app_author")
        with baseline_file({BASELINED_ID: {author_sql: count}}):
            result = run_inner(baselined_case(plant_two_repeats))
        message = only_failure(result)
        assert "queryguard_app_book" in message
        # And the excused shape is NOT in the failure, which is what makes this a
        # per-shape allowance rather than a per-test one that happens to fail.
        assert "queryguard_app_author" not in message

    def test_the_failure_names_the_baseline_file(self):
        seed_books()
        with baseline_file() as path:
            result = run_inner(baselined_case(plant_nplusone))
        assert path.name in only_failure(result)

    def test_the_failure_mentions_no_baseline_when_none_is_configured(self):
        # Telling a project not to hand-edit a file it does not have is noise in
        # the middle of the one message that has to be actionable.
        seed_books()
        message = only_failure(run_inner(baselined_case(plant_nplusone)))
        assert "hand-edit" not in message
        assert "repeated query shape" in message

    def test_an_inner_class_is_not_excused_by_a_hand_added_entry(self):
        # Excluded on read as well as on write, so a hand-edited file cannot mute a
        # positive control. A REAL `<locals>` id, not a synthetic one.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()

        author_sql, _ = one_shape(measure(touch_authors), "queryguard_app_author")
        with baseline_file({Inner("runTest").id(): {author_sql: 99}}):
            result = run_inner(Inner)
        assert "repeated query shape" in only_failure(result)

    def test_an_inner_class_is_not_excused_by_an_update_run_either(self):
        # Otherwise the regeneration command is itself red: the positive controls
        # assert the inner test fails, and collecting instead of failing makes every
        # one of them report a pass.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            def runTest(self):
                touch_authors()

        with baseline_file(), updating():
            result = run_inner(Inner)
        assert "repeated query shape" in only_failure(result)

    def test_a_baselinable_test_IS_excused_by_an_update_run(self):
        # The control for the two above: an update run has to collect rather than
        # fail, or regeneration is impossible.
        seed_books()
        with baseline_file(), updating():
            result = run_inner(baselined_case(plant_nplusone))
        assert result.wasSuccessful(), result.failures
        assert [test_id for test_id, _ in queryguard._FINDINGS] == [BASELINED_ID]


class ExpectRepeatsTests(TestCase):
    """The ``@allow_repeats`` counterpart: an exemption that cannot rot."""

    def test_the_decorator_marks_both_attributes(self):
        # The allowance *and* the expectation. Without `_allow_repeats` the
        # decorated test would fail as an ordinary finding and never reach the
        # expectation at all.
        @expect_repeats(4)
        def method():
            pass

        assert method._allow_repeats == 4
        assert method._expect_repeats == 4

    def test_expected_repeats_for_reads_the_class_not_the_instance(self):
        # Same reason threshold_for does: Django replaces the instance attribute
        # with an AsyncToSync wrapper for a coroutine test, and that wrapper
        # carries neither marker.
        class Inner(SimpleTestCase):
            @expect_repeats(3)
            def runTest(self):
                pass

        test = Inner("runTest")
        test.runTest = lambda: None  # what Django does for an async test
        assert expected_repeats_for(test) == 3

    def test_fail_expectation_always_raises(self):
        # `check` relies on this: there is no `return` after the call, so a
        # non-raising version would fall through into the offenders path and report
        # twice. Pinned here rather than assumed at the call site.
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        with pytest.raises(AssertionError):
            queryguard.fail_expectation(Inner("runTest"), "a.B.test_c", 3, 1)

    def test_an_undecorated_test_expects_nothing(self):
        class Inner(SimpleTestCase):
            def runTest(self):
                pass

        assert expected_repeats_for(Inner("runTest")) is None

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_a_pinned_nplusone_passes(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(3)
            def runTest(self):
                touch_authors()

        result = run_inner(Inner)
        assert result.wasSuccessful(), result.failures

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_a_pinned_nplusone_that_was_fixed_fails(self):
        # The whole point. @allow_repeats would have stayed green here forever,
        # with the ceiling still raised.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(3)
            def runTest(self):
                touch_authors_eagerly()

        message = only_failure(run_inner(Inner))
        assert "@expect_repeats(3)" in message
        assert "remove the decorator" in message

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_the_failure_names_the_count_actually_observed(self):
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(9)
            def runTest(self):
                touch_authors()

        assert "more than 3 time(s)" in only_failure(run_inner(Inner))

    @override_settings(QUERY_GUARD_REPORT_ONLY=True)
    def test_it_fails_in_report_only_mode_too(self):
        # Report-only exists so a *blanket* detector does not redden a suite nobody
        # has triaged. This is an assertion the author wrote by hand about one
        # test, and silencing it would make the decorator a synonym for
        # @allow_repeats in the mode most projects start in.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(3)
            def runTest(self):
                touch_authors_eagerly()

        assert "@expect_repeats(3)" in only_failure(run_inner(Inner))

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_a_repeat_deeper_than_expected_still_fails_as_a_finding(self):
        # The decorator raises the ceiling to exactly `count`; it is not a licence
        # to get worse.
        seed_books(9)

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(2)
            def runTest(self):
                touch_authors()

        message = only_failure(run_inner(Inner))
        assert "repeated query shape" in message
        assert "@expect_repeats" not in message

    @override_settings(QUERY_GUARD_REPORT_ONLY=False)
    def test_a_partial_fix_still_fails_the_expectation(self):
        # The boundary a mutation round found unguarded. The two tests above sit at
        # "gone entirely" (worst 1) and "still there" (worst == expected), so
        # `worst < expected` and `worst < expected - 1` are indistinguishable to
        # both. This is the case between them: the N+1 got *shallower* without
        # going away, and the decorator has to notice.
        seed_books(4)
        author_sql, count = one_shape(measure(touch_authors), "queryguard_app_author")
        assert count == 4, count

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(5)
            def runTest(self):
                touch_authors()

        with override_settings(QUERY_GUARD_REPORT_ONLY=False):
            message = only_failure(run_inner(Inner))
        assert "@expect_repeats(5)" in message
        assert "more than 4 time(s)" in message

    def test_it_does_not_fire_on_a_body_that_raised(self):
        # An incomplete run measured fewer queries than the test earns, so "it
        # stopped repeating" is not something this run can know -- and piling the
        # decorator's failure on top would bury the real one.
        seed_books()

        class Inner(QueryGuardMixin, SimpleTestCase):
            databases = {"default"}

            @expect_repeats(3)
            def runTest(self):
                self.fail("something unrelated")

        message = only_failure(run_inner(Inner))
        assert "something unrelated" in message
        assert "@expect_repeats" not in message


def guarded_suite(*cases):
    return unittest.TestSuite(cases)


def nplusone_case(test_id):
    """A discoverable-id case that plants an N+1, for runner-level tests."""

    class Planted(SimpleTestCase):
        databases = {"default"}

        def id(self):
            return test_id

        def test_nplusone(self):
            touch_authors()

    return Planted("test_nplusone")


def clean_case(test_id):
    class Clean(SimpleTestCase):
        databases = {"default"}

        def id(self):
            return test_id

        def test_eager(self):
            touch_authors_eagerly()

    return Clean("test_eager")


def run_through_runner(runner, suite):
    """Run ``suite`` through ``runner``, returning (result, stderr)."""
    stderr = io.StringIO()
    with unittest.mock.patch("sys.stderr", stderr):
        result = runner.run_suite(suite)
    return result, stderr.getvalue()


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class ObservedTests(TestCase):
    """``_OBSERVED`` is what the ratchet is written and judged from."""

    def test_a_clean_test_is_recorded_as_an_empty_entry(self):
        # "Ran and repeated nothing" has to be a different state from "did not
        # run": the first is evidence for deleting an entry, the second is not.
        seed_books()
        run_inner(baselined_case(lambda self: None if touch_authors_eagerly() else None))
        assert queryguard._OBSERVED == {BASELINED_ID: {}}

    def test_a_repeating_test_records_its_shapes(self):
        seed_books()
        with baseline_file():
            run_inner(baselined_case(plant_nplusone))
        recorded = queryguard._OBSERVED[BASELINED_ID]
        assert any("queryguard_app_author" in sql for sql in recorded)

    def test_a_body_that_raised_is_not_recorded_at_all(self):
        # An under-count recorded here is wrong in both directions: the stale
        # report calls a still-earned entry unnecessary, and an update run DELETES
        # it, since merge_baseline carries only entries for tests it did not see.
        seed_books()

        def body(self):
            touch_authors()
            self.fail("something unrelated")

        run_inner(baselined_case(body))
        assert BASELINED_ID not in queryguard._OBSERVED

    def test_a_test_judged_against_another_baseline_is_not_recorded(self):
        # Such a test is not part of the run being baselined: its findings must not
        # be written into the run's file.
        seed_books()
        with baseline_file() as run_path:
            queryguard._RUN_ACTIVE = True
            queryguard._RUN_BASELINE_PATH = run_path
            with baseline_file():  # a different temp directory
                run_inner(baselined_case(plant_nplusone))
        assert queryguard._OBSERVED == {}

    def test_a_test_on_the_runs_own_baseline_is_recorded(self):
        # The control: without it, the test above passes on a bug that records
        # nothing ever.
        seed_books()
        with baseline_file() as run_path:
            queryguard._RUN_ACTIVE = True
            queryguard._RUN_BASELINE_PATH = run_path
            run_inner(baselined_case(plant_nplusone))
        assert BASELINED_ID in queryguard._OBSERVED

    def test_no_runner_means_every_test_is_on_the_runs_baseline(self):
        # The mixin used on its own, with no runner to scope anything.
        assert queryguard._RUN_ACTIVE is False
        with baseline_file():
            assert queryguard.judged_against_run_baseline() is True


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class UpdateRunTests(TestCase):
    """``QUERY_GUARD_UPDATE_BASELINE=1`` — the only sanctioned way the file changes."""

    def test_an_update_run_writes_what_it_measured(self):
        seed_books()
        with baseline_file() as path, updating():
            _, printed = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case("tests.m.C.test_a")))
            written = json.loads(path.read_text())
        assert list(written) == ["tests.m.C.test_a"]
        assert any("queryguard_app_author" in sql for sql in written["tests.m.C.test_a"])
        assert f"wrote {path} (1 entries)" in printed

    def test_an_update_run_does_not_fail_the_tests_it_measures(self):
        seed_books()
        with baseline_file(), updating():
            result, _ = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case("tests.m.C.test_a")))
        assert result.wasSuccessful(), result.failures

    def test_a_clean_test_earns_no_entry(self):
        seed_books()
        with baseline_file() as path, updating():
            run_through_runner(QueryGuardRunner(), guarded_suite(clean_case("tests.m.C.test_b")))
            assert json.loads(path.read_text()) == {}

    def test_a_fixed_test_loses_its_entry_so_the_ratchet_tightens(self):
        seed_books()
        with baseline_file({"tests.m.C.test_b": {SELECT_AUTHOR: 9}}) as path, updating():
            run_through_runner(QueryGuardRunner(), guarded_suite(clean_case("tests.m.C.test_b")))
            assert json.loads(path.read_text()) == {}

    def test_a_partial_run_carries_the_entries_it_did_not_exercise(self):
        # Deleting them reads in review exactly like the ratchet tightening, while
        # actually disarming every test the run did not reach.
        seed_books()
        existing = {"tests.m.Elsewhere.test_z": {SELECT_AUTHOR: 4}}
        with baseline_file(existing) as path, updating():
            _, printed = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case("tests.m.C.test_a")))
            written = json.loads(path.read_text())
        assert written["tests.m.Elsewhere.test_z"] == {SELECT_AUTHOR: 4}
        assert "carried 1 entry" in printed
        assert "regenerate with the full suite" in printed

    def test_unreadable_entries_are_reported_on_the_wrote_line(self):
        # Not in a note above it. `wrote (N entries)` after a partial migration
        # reads exactly like an ordinary ratchet tightening, so the number a reader
        # looks at has to carry the number they would regret not seeing.
        seed_books()
        with baseline_file({"tests.m.Old.test_y": 3}) as path, updating():
            _, printed = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case("tests.m.C.test_a")))
        wrote_line = next(line for line in printed.splitlines() if "wrote" in line)
        assert "DISCARDED 1 unreadable entry" in wrote_line
        assert str(path) in wrote_line

    def test_a_mixed_file_reports_both_the_discard_and_the_carry(self):
        # `+=`, not `=`. An assignment silently drops the discard warning in
        # exactly the case where it matters most.
        seed_books()
        existing = {"tests.m.Old.test_y": 3, "tests.m.Elsewhere.test_z": {SELECT_AUTHOR: 4}}
        with baseline_file(existing), updating():
            _, printed = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case("tests.m.C.test_a")))
        wrote_line = next(line for line in printed.splitlines() if "wrote" in line)
        assert "DISCARDED 1 unreadable entry" in wrote_line
        assert "carried 1 entry" in wrote_line

    def test_the_plural_forms_agree_with_the_counts(self):
        seed_books()
        existing = {"a": 3, "b": 3, "c": {SELECT_AUTHOR: 4}, "d": {SELECT_AUTHOR: 4}}
        with baseline_file(existing), updating():
            _, printed = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case("tests.m.C.test_a")))
        assert "DISCARDED 2 unreadable entries" in printed
        assert "carried 2 entries" in printed

    def test_a_run_that_observed_nothing_refuses_to_blank_an_existing_file(self):
        # A blanked baseline is a *passing* suite until the next real regeneration
        # — the quietest possible way for this mechanism to stop working.
        with baseline_file({"tests.m.C.test_a": {SELECT_AUTHOR: 2}}) as path, updating():
            with pytest.raises(SystemExit) as caught:
                run_through_runner(QueryGuardRunner(), guarded_suite())
            assert json.loads(path.read_text()) == {"tests.m.C.test_a": {SELECT_AUTHOR: 2}}
        message = str(caught.value)
        assert "observed no tests" in message
        assert REGENERATE_COMMAND in message

    def test_a_run_that_observed_nothing_may_create_a_file_that_is_not_there(self):
        # Nothing is at risk: there is no recorded state to lose, and refusing here
        # would make a brand-new baseline impossible to bootstrap from an empty
        # selection.
        with baseline_file() as path, updating():
            run_through_runner(QueryGuardRunner(), guarded_suite())
            assert json.loads(path.read_text()) == {}

    def test_an_update_run_does_not_excuse_a_test_on_a_different_baseline(self):
        # A test pointing QUERY_GUARD_BASELINE somewhere else is not part of the run
        # being baselined, so the update run must judge it rather than collect it —
        # otherwise regenerating one project's baseline silently excuses every test
        # that manages its own.
        seed_books()
        with baseline_file() as run_path, updating():
            queryguard._RUN_ACTIVE = True
            queryguard._RUN_BASELINE_PATH = run_path
            with baseline_file():  # a different, empty file
                result = run_inner(baselined_case(plant_nplusone))
        assert "repeated query shape" in only_failure(result)

    def test_an_update_run_with_no_baseline_configured_is_refused(self):
        # The library difference: there is no implicit default, so "write it" has
        # to name where.
        with updating():
            with pytest.raises(ImproperlyConfigured) as caught:
                QueryGuardRunner().run_suite(guarded_suite())
        message = str(caught.value)
        assert "QUERY_GUARD_BASELINE" in message
        assert "committed" in message

    def test_the_refusal_happens_before_the_suite_runs(self):
        ran = []
        with updating():
            with unittest.mock.patch.object(unittest.TextTestRunner, "run", lambda self, suite: ran.append(1)):
                with pytest.raises(ImproperlyConfigured):
                    QueryGuardRunner().run_suite(guarded_suite())
        assert ran == []

    def test_an_update_run_under_parallel_is_refused(self):
        # Findings are per worker process and nothing gathers them back, so the
        # file would record whichever worker wrote last. Unlike the enforcement
        # side, where the same mechanism costs a false green a serial run corrects,
        # this one writes the loss to disk.
        with baseline_file(), updating():
            runner = QueryGuardRunner()
            runner.parallel = 4
            with pytest.raises(ImproperlyConfigured) as caught:
                runner.run_suite(guarded_suite())
        assert "--parallel" in str(caught.value)

    def test_a_serial_update_run_is_not_refused(self):
        # The control: `parallel` is 0 or 1 on a serial runner and the refusal must
        # not fire on the common path.
        seed_books()
        with baseline_file(), updating():
            runner = QueryGuardRunner()
            runner.parallel = 1
            result, _ = run_through_runner(runner, guarded_suite(clean_case("tests.m.C.test_b")))
        assert result.wasSuccessful()

    def test_the_restore_happens_even_when_settling_raises(self):
        # `settle_baseline` raises SystemExit on an empty update run, and a restore
        # placed after it is then skipped — leaving `_RUN_BASELINE_PATH` pointing at
        # this run's file for every later test in the process.
        with baseline_file({"a": {SELECT_AUTHOR: 2}}), updating():
            with pytest.raises(SystemExit):
                run_through_runner(QueryGuardRunner(), guarded_suite())
        assert queryguard._RUN_ACTIVE is False
        assert queryguard._RUN_BASELINE_PATH is None

    def test_a_nested_runner_restores_the_outer_runs_baseline(self):
        with baseline_file() as outer:
            queryguard._RUN_ACTIVE = True
            queryguard._RUN_BASELINE_PATH = outer
            with baseline_file():
                run_through_runner(QueryGuardRunner(), guarded_suite())
            assert queryguard._RUN_ACTIVE is True
            assert queryguard._RUN_BASELINE_PATH == outer


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class StaleBaselineGateTests(TestCase):
    """An entry that is no longer earned has to go red, not just print."""

    def _stale_run(self, runner=None):
        seed_books()
        runner = runner or QueryGuardRunner()
        suite = guarded_suite(clean_case("tests.m.C.test_b"))
        with baseline_file({"tests.m.C.test_b": {SELECT_AUTHOR: 4}}):
            result, printed = run_through_runner(runner, suite)
            failures = runner.suite_result(suite, result)
        return runner, result, printed, failures

    def test_a_stale_entry_is_named_on_stderr(self):
        _, _, printed, _ = self._stale_run()
        assert "tests.m.C.test_b" in printed
        assert "no longer needed" in printed
        assert REGENERATE_COMMAND in printed

    def test_a_stale_entry_is_recorded_on_the_runner(self):
        runner, _, _, _ = self._stale_run()
        assert runner.stale_baseline_entries == ["tests.m.C.test_b"]

    def test_a_stale_entry_fails_the_run(self):
        # Without this a fix that tightens the ratchet lands while the file still
        # records the old number, and the next PR to regenerate ships deletions
        # nobody in it caused.
        _, result, _, failures = self._stale_run()
        assert result.wasSuccessful()
        assert failures == 1

    def test_the_failing_message_says_the_tests_themselves_passed(self):
        runner, result, _, _ = self._stale_run()
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            runner.suite_result(unittest.TestSuite(), result)
        assert "The tests themselves passed" in stderr.getvalue()

    def test_the_message_does_not_claim_a_pass_when_there_were_failures(self):
        # Printed directly under `FAILED`, where "the tests passed" is a plain lie.
        runner, result, _, _ = self._stale_run()
        result.failures.append((None, "a real failure"))
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            counted = runner.suite_result(unittest.TestSuite(), result)
        printed = stderr.getvalue()
        assert "The tests themselves passed" not in printed
        assert "Separately from the failures above" in printed
        assert counted == 2

    @override_settings(QUERY_GUARD_REPORT_ONLY=True)
    def test_report_only_mode_reports_but_does_not_fail(self):
        # A stale entry is a control that is *tighter* than recorded, and someone
        # mid-fix should not be blocked by it.
        runner, result, printed, failures = self._stale_run()
        assert runner.stale_baseline_entries == ["tests.m.C.test_b"]
        assert failures == 0
        assert "no longer needed" in printed

    def test_a_reused_runner_drops_a_previous_runs_stale_verdict(self):
        # `settle_baseline` resets both fields on entry. Without that, a runner
        # reused across two invocations carries the first run's verdict into the
        # second and fails a run that is genuinely settled. A mutation round found
        # this unguarded: every other test here builds a fresh runner, so the reset
        # could be deleted with the suite green.
        seed_books()
        runner, _, _, _ = self._stale_run()
        assert runner.stale_baseline_entries == ["tests.m.C.test_b"]

        suite = guarded_suite(clean_case("tests.m.C.test_b"))
        with baseline_file():  # a fresh, empty baseline: nothing can be stale
            result, _ = run_through_runner(runner, suite)
        assert runner.stale_baseline_entries == []
        assert runner.stale_baseline_path is None
        assert runner.suite_result(suite, result) == 0

    def test_a_still_earned_entry_is_not_reported(self):
        seed_books()
        author_sql, count = one_shape(measure(touch_authors), "queryguard_app_author")
        runner = QueryGuardRunner()
        suite = guarded_suite(nplusone_case("tests.m.C.test_a"))
        with baseline_file({"tests.m.C.test_a": {author_sql: count}}):
            result, printed = run_through_runner(runner, suite)
        assert runner.stale_baseline_entries == []
        assert "no longer needed" not in printed
        assert runner.suite_result(suite, result) == 0

    def test_the_runner_starts_with_no_stale_entries(self):
        # A class attribute as well as an instance one, so `suite_result` is safe on
        # a runner whose `run_suite` never got that far.
        assert QueryGuardRunner.stale_baseline_entries == []
        assert QueryGuardRunner().suite_result(unittest.TestSuite(), unittest.TestResult()) == 0

    def test_recording_a_stale_entry_does_not_leak_to_the_next_runner(self):
        # Rebound, never mutated in place: an `.append()` would write through to the
        # shared class-level list and become process-global.
        runner, _, _, _ = self._stale_run()
        assert runner.stale_baseline_entries
        assert QueryGuardRunner.stale_baseline_entries == []
        assert QueryGuardRunner().stale_baseline_entries == []

    def test_an_unreadable_entry_is_reported_rather_than_raised_from_settling(self):
        # The run is already loudly red from `check()`; raising again here only
        # buries that under a traceback from the summary.
        seed_books()
        with baseline_file({"tests.m.Old.test_y": 3}) as path:
            _, printed = run_through_runner(QueryGuardRunner(), guarded_suite(clean_case("tests.m.C.test_b")))
        assert "1 unreadable entry is" in printed
        assert str(path) in printed

    def test_the_stale_check_says_it_is_inert_under_parallel(self):
        # `_OBSERVED` lives in the worker processes. A gate that silently does
        # nothing under a documented command is worse than no gate.
        seed_books()
        runner = QueryGuardRunner()
        runner.parallel = 4
        with baseline_file({"tests.m.C.test_b": {SELECT_AUTHOR: 4}}):
            _, printed = run_through_runner(runner, guarded_suite(clean_case("tests.m.C.test_b")))
        assert "INERT under --parallel" in printed
        assert runner.stale_baseline_entries == []

    def test_the_gate_still_reports_when_the_setting_has_since_been_unset(self):
        # `suite_result` runs after `run_suite` has returned, so the setting can
        # have changed by then. Re-reading it was an AttributeError out of the one
        # branch whose whole job is to report — a traceback instead of a verdict.
        runner, result, _, _ = self._stale_run()
        assert runner.stale_baseline_path is not None
        runner.stale_baseline_path = None
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            counted = runner.suite_result(unittest.TestSuite(), result)
        assert counted == 1
        assert "the baseline" in stderr.getvalue()

    def test_the_message_names_the_file_the_verdict_came_from(self):
        seed_books()
        runner = QueryGuardRunner()
        suite = guarded_suite(clean_case("tests.m.C.test_b"))
        with baseline_file({"tests.m.C.test_b": {SELECT_AUTHOR: 4}}, name="pinned.json") as path:
            result, _ = run_through_runner(runner, suite)
        assert runner.stale_baseline_path == path
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            runner.suite_result(suite, result)
        assert "pinned.json" in stderr.getvalue()

    def test_nothing_is_settled_when_no_baseline_is_configured(self):
        seed_books()
        runner = QueryGuardRunner()
        suite = guarded_suite(clean_case("tests.m.C.test_b"))
        _, printed = run_through_runner(runner, suite)
        assert "no longer needed" not in printed
        assert "unreadable" not in printed
        assert runner.stale_baseline_path is None
        assert runner.suite_result(suite, unittest.TestResult()) == 0


@override_settings(QUERY_GUARD_REPORT_ONLY=False)
class BaselineLifecycleTests(TestCase):
    """One test that walks the whole claim, end to end, through the runner.

    Every test above checks one link. This checks that the links compose, because
    the feature's promise is a *sequence* — red, regenerate, green, fix, stale,
    regenerate — and each step passing in isolation says nothing about the step
    after it seeing the file the step before it wrote.
    """

    def test_red_then_baselined_then_fixed_then_stale_then_clean(self):
        seed_books()
        planted = "tests.m.Dashboard.test_lists_authors"

        with baseline_file() as path:
            # 1. Enforcing with no baseline: the planted N+1 fails. This is the
            #    state an adopter is in on the day they set REPORT_ONLY = False.
            result, _ = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case(planted)))
            assert len(result.failures) == 1, result.failures

            # 2. Regenerate. The file now records the shape and the count.
            with updating():
                run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case(planted)))
            recorded = json.loads(path.read_text())
            assert list(recorded) == [planted]
            (author_sql,) = [sql for sql in recorded[planted] if "queryguard_app_author" in sql]

            # 3. The same suite is now green — the existing N+1 is recorded, not
            #    fixed, and that is the whole point of a ratchet.
            runner = QueryGuardRunner()
            suite = guarded_suite(nplusone_case(planted))
            result, printed = run_through_runner(runner, suite)
            assert result.wasSuccessful(), result.failures
            assert runner.suite_result(suite, result) == 0

            # 4. It is a ratchet, not a mute button: getting worse still fails.
            Book.objects.create(title="one more", author=Author.objects.first())
            result, _ = run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case(planted)))
            assert len(result.failures) == 1, result.failures
            assert f"(allowed {recorded[planted][author_sql]})" in result.failures[0][1]

            # 5. Fix the N+1 -- same test id, eager loading now -- and the entry it
            #    no longer earns FAILS the run rather than merely printing.
            runner = QueryGuardRunner()
            suite = guarded_suite(clean_case(planted))
            result, printed = run_through_runner(runner, suite)
            assert result.wasSuccessful(), result.failures
            assert runner.stale_baseline_entries == [planted]
            assert runner.suite_result(suite, result) == 1
            assert "no longer needed" in printed

            # 6. Regenerate again: the entry is dropped, and the run is finally both
            #    green and settled. The ratchet has tightened and cannot loosen.
            with updating():
                run_through_runner(QueryGuardRunner(), guarded_suite(clean_case(planted)))
            assert json.loads(path.read_text()) == {}

            runner = QueryGuardRunner()
            suite = guarded_suite(clean_case(planted))
            result, _ = run_through_runner(runner, suite)
            assert result.wasSuccessful()
            assert runner.suite_result(suite, result) == 0

    def test_a_second_run_of_the_same_test_id_still_counts_as_observed(self):
        # `len(_OBSERVED)` cannot see this: the dict is keyed by test id, so
        # re-observing one leaves the length unchanged and the update path refuses
        # to write, reporting a real run as having bracketed nothing. The lifecycle
        # test above is where that first fired; this is the isolated statement of
        # it, so a regression names the cause rather than a six-step sequence.
        seed_books()
        planted = "tests.m.Dashboard.test_lists_authors"
        with baseline_file() as path, updating():
            run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case(planted)))
            first = json.loads(path.read_text())
            assert list(first) == [planted]
            # Same id, second invocation in the same process.
            run_through_runner(QueryGuardRunner(), guarded_suite(nplusone_case(planted)))
            assert json.loads(path.read_text()) == first
