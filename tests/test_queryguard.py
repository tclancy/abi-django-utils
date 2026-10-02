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
import threading
import unittest
from contextlib import ExitStack

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import request_finished, request_started
from django.db import connections
from django.test import AsyncClient, Client, SimpleTestCase, TestCase, override_settings

from abi_django_utils import queryguard
from abi_django_utils.queryguard import (
    GuardedParallelTestSuite,
    QueryCollector,
    QueryGuardMixin,
    QueryGuardRunner,
    allow_repeats,
    check,
    default_threshold,
    emit_report,
    format_finding,
    format_report,
    guarding,
    install,
    is_select,
    new_window,
    report_only,
    threshold_for,
    warm_content_types,
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
    for leaked in list(queryguard._ACTIVE):
        leaked.close()
    yield
    queryguard._FINDINGS.clear()
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

    def test_a_finding_with_a_threshold_names_the_allowance(self):
        rendered = format_finding("a.B.test_c", {SELECT_AUTHOR: 3}, 1)
        assert "3x (allowed 1)" in rendered

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
