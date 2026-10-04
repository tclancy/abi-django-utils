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
    sensitivity that catches a real N+1 over three fixture rows. Deliberately not
    raised to quiet an existing suite: an N+1 is invisible at fixture scale
    precisely because three rows means a handful of queries, so a threshold high
    enough to silence a suite is a threshold high enough to miss the thing being
    hunted. The baseline is what that job belongs to.

``QUERY_GUARD_BASELINE`` (no default — unset means no baseline)
    Path to the baseline file. See below. **There is no default on purpose**: this
    module is installed into your virtualenv, so a path derived from its own
    location would put a generated artifact where it is unreviewable and is
    deleted by the next sync. Point this at a path inside your repository.

The baseline
------------

Switching a blanket detector on over an existing suite has two bad answers —
leave it in report-only, where nobody reads it, or clear every finding before
anything is protected — and one good one. The **baseline** is a committed record
of what each test *already* repeats, so the guard fails on anything new from the
day it lands::

    QUERY_GUARD_BASELINE = BASE_DIR / "queryguard_baseline.json"
    QUERY_GUARD_REPORT_ONLY = False

Each entry records, **per query shape**, the count that test already repeats, and
raises the allowance for that shape and no further. A baselined test that gets
worse still fails, so the file is a ratchet rather than a mute button: it can only
be tightened by fixing a test, never loosened by accident.

Per *shape*, not per test, and that distinction is the difference between a
ratchet and a hole. An allowance applied to every shape in a test lets a brand-new
2x repeat of a completely unrelated query land inside it silently. Naming the shape
also makes a regenerated file readable a second way — the diff shows *which* query
a fix removed, not only that a number went down.

Generate it, and commit the result::

    QUERY_GUARD_UPDATE_BASELINE=1 manage.py test

That writes the file instead of failing the run, and it is the only way the file
should ever change. Hand-editing it to quiet a new finding is the one thing this
design cannot stop you doing, and it defeats the whole mechanism.

Regenerating after a fix is **not optional**: an enforcing run that proves an
entry is no longer earned *fails*, naming the entries and the command. Otherwise a
fix can tighten the ratchet without the file following, and the next PR to
regenerate ships deletions nobody in it caused. In report-only mode that stays a
note — someone mid-fix should not be blocked by a control tighter than the file
records.

Writing the file, carrying entries and the stale gate all live on
``QueryGuardRunner``, so **the baseline needs the runner** — none of it happens
under ``QueryGuardMixin`` alone, and setting ``QUERY_GUARD_UPDATE_BASELINE`` with
no runner active says so on stderr rather than quietly standing enforcement down.

Four refusals protect the file from an update run that looks like it worked: no
baseline configured; a baseline path whose directory does not exist; ``--parallel``,
where findings live in worker processes nothing gathers back; and a run that
bracketed no tests at all, which would blank an existing file. A blanked baseline
is a *passing* suite until the next real regeneration.

A partial update run — one module rather than the suite — carries the entries it
did not exercise rather than deleting them, and says how many it carried. Deleting
them reads in review exactly like the ratchet tightening while actually disarming
every test the run did not reach.

Pinning a known-bad path instead
--------------------------------

``@allow_repeats(n)`` is an exemption, and it is permanent: the day somebody fixes
the N+1 it covered, the decorator stays, the ceiling stays raised, and a new repeat
of any shape in that test is silently inside it. ``@expect_repeats(n)`` is the same
allowance plus an assertion that it is still needed::

    @expect_repeats(4)
    def test_the_dashboard_still_queries_per_row(self):
        ...

Fix the N+1 and that test fails, naming the decorator to remove. Prefer it for a
known N+1 you are not fixing today; prefer ``@allow_repeats`` only where the
repetition is genuinely *intended*, and the baseline where you want the shape
itself recorded.

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
it.

**The dividing line is how the event loop is entered, not sync versus async.**
``sync_to_async``'s default ``thread_sensitive=True`` means "run on the thread
the outer synchronous caller is on" — and when there is no outer synchronous
caller it falls through to a shared single-worker ``ThreadPoolExecutor``
instead. Measured on asgiref 3.11 / Django 6.0, thread-sensitive work lands on:

=========================================  ====================  =========
entry point                                thread                covered
=========================================  ====================  =========
``async_to_sync(coro)()``                  ``MainThread``        yes
Django ``async def test_*``                ``MainThread``        yes
``asyncio.run(coro)``                      pool worker           **no**
``asyncio.run`` + ``ThreadSensitiveContext``  pool worker        **no**
=========================================  ====================  =========

So, concretely:

* **Covered:** sync tests; ``async`` views reached through ``AsyncClient``;
  Django's own ``async def test_*`` methods, because ``SimpleTestCase`` wraps
  them in ``async_to_sync`` before calling them; Django's async ORM
  (``async for``, ``aget``) inside any of those.
* **Not covered:** a bare ``asyncio.run()`` in a test body, ``pytest-asyncio``,
  ``unittest.IsolatedAsyncioTestCase`` (which runs its own loop *and* overrides
  ``_callTestMethod``, so the guard is never even entered),
  ``sync_to_async(..., thread_sensitive=False)``, and a bare
  ``threading.Thread``.

The uncovered cases fail **silently** — the guard records an empty window and
reports a pass. That includes on SQLite: Django's test database for
``NAME = ":memory:"`` is ``file:memorydb_default?mode=memory&cache=shared``, so
another thread shares the same tables rather than finding an empty database.
There is no fix available from inside an ``execute_wrapper``, so this is
documented and pinned by ``AsyncCoverageTests`` rather than worked around.

Report-only mode under ``--parallel``
-------------------------------------

Findings are collected in a module-global list, which is per *process*.
``--parallel`` workers are separate processes and nothing gathers their lists
back, so report-only mode under ``--parallel`` yields a partial report per
worker at pool teardown rather than one consolidated report. Enforcing mode is
unaffected: failures travel home in the test result. Generate your first report
serially.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import unittest
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import request_finished, request_started
from django.db import connections
from django.test.runner import DiscoverRunner, ParallelTestSuite

# The published surface, kept to what README.md documents plus the names the
# baseline adds for a user. Deliberately NOT every helper the tests import: this
# is an installed library, so each name here is a semver commitment, and the
# baseline's internals (`write_baseline`, `merge_baseline`, `stale_entries`,
# `unbaselined_shapes`, `carryable_baseline`, `observation_count`, …) are
# implementation. `__all__` does not restrict `from … import <name>`, so the tests
# reach them regardless and nothing is lost by not advertising them.
__all__ = [
    "REGENERATE_COMMAND",
    "LegacyBaselineFormat",
    "QueryCollector",
    "QueryGuardMixin",
    "QueryGuardRunner",
    "UnreadableBaselineEntry",
    "allow_repeats",
    "baseline_path",
    "emit_report",
    "expect_repeats",
    "format_finding",
    "format_report",
    "guarding",
    "install",
    "is_select",
    "new_window",
]

#: Findings accumulated across the run while in report-only mode.
_FINDINGS: list[tuple[str, dict[str, int]]] = []

#: Every test the guard actually bracketed this run, and the worst repeat count
#: it saw for each *shape*. Populated whatever the mode, because the baseline
#: writer and the stale-entry report both need the tests that produced *no*
#: finding — which by definition never reach ``_FINDINGS``. A test that ran clean
#: maps to an empty dict, which is how "ran and repeated nothing" is told apart
#: from "did not run". The ratchet depends on that distinction: deleting an entry
#: is how it tightens, and only a test that ran is evidence for a deletion.
_OBSERVED: dict[str, dict[str, int]] = {}

#: How many times ``check`` has recorded an observation this process. A counter
#: rather than ``len(_OBSERVED)``, because the runner's "did this run observe
#: anything?" question is answered by a **delta** and that dict is keyed by test
#: id: re-observing an id already in it leaves the length unchanged, so a real run
#: reads as having bracketed nothing and the update path then refuses to write.
#: Monotonic, never reset except by a test, so the delta is always meaningful.
_OBSERVATIONS = 0


def observation_count() -> int:
    """How many observations ``check`` has recorded this process.

    A function rather than two reads of the global, so the runner's before/after
    pair cannot end up computed from two different quantities — which is exactly
    what a half-applied fix produces, and the delta is then a nonsense number that
    is still non-zero and so passes the "observed nothing" guard.
    """
    return _OBSERVATIONS


#: Whether a ``QueryGuardRunner`` is mid-``run_suite``, and the baseline that run
#: is using. Two variables rather than one, because ``None`` is already a
#: meaningful *path* here — it is how "no baseline configured" is spelled — so a
#: single slot could not tell that apart from "no runner active".
#:
#: A test may point ``QUERY_GUARD_BASELINE`` somewhere else (the guard's own
#: tests do, at a temp file) and such a test is not part of the run being
#: baselined: it must not be excused by an update run, and its findings must not
#: be written into the run's file.
_RUN_ACTIVE = False
_RUN_BASELINE_PATH: Path | None = None

#: The one spelling of the regeneration command, interpolated into every refusal
#: that prescribes it. A constant rather than eight copies because every one of
#: those messages is a promise that this exact command fixes the thing being
#: refused — and the itemshop original, which spelled it out each time, spelled
#: it ``manage.py test items``, naming an app this library has never heard of.
REGENERATE_COMMAND = "QUERY_GUARD_UPDATE_BASELINE=1 manage.py test"

#: Collectors currently recording, so ``new_window()`` can reach them without
#: being handed one. A list rather than a single slot because ``guarding()`` is
#: re-entrant and the guard's own tests build collectors directly; under normal
#: use it holds at most one.
#:
#: ``_ACTIVE_LOCK`` protects **this registry only** — a thread whose queries
#: cannot be measured can still call ``new_window()``, and iterating a list
#: another thread is appending to is the one unsafe operation here that has no
#: upside. A collector's own ``shapes``/``windows`` are deliberately left
#: unsynchronised: the wrapper runs on whichever thread issued the query, and
#: that thread is by definition one whose connection the guard wrapped, i.e. the
#: same thread the test body is on. The worst case if that ever stops holding is
#: a query attributed to the neighbouring window — a missed or invented finding,
#: not corruption — and paying a lock per query to narrow it is the wrong
#: trade in a test-only hot path.
_ACTIVE: list[QueryCollector] = []
_ACTIVE_LOCK = threading.Lock()

#: Reported at most once per process — see ``warm_content_types``.
_WARM_FAILURE_REPORTED = False

#: Reported at most once per process — see ``warn_update_without_runner``.
_UPDATE_WITHOUT_RUNNER_REPORTED = False


def _forbidden_query_error() -> type[BaseException] | tuple[()]:
    """Django's "no database queries in a SimpleTestCase" error.

    Resolved by name because it is not part of Django's public API surface. A
    tuple fallback of ``()`` is deliberate: ``except ()`` catches nothing, so if
    Django ever moves it the warm falls through to the loud ``except`` below
    rather than silently swallowing everything.
    """
    try:
        from django.test.testcases import DatabaseOperationForbidden

        return DatabaseOperationForbidden
    except ImportError:  # pragma: no cover - only on an unexpected Django
        return ()


_ForbiddenQuery = _forbidden_query_error()


def allow_repeats(count: int):
    """Raise the repeated-shape threshold for a single test.

    For the genuinely deliberate cases — a test that loops on purpose to prove
    pagination, say — rather than as a way to quiet a real finding.
    """

    def decorator(func):
        func._allow_repeats = count
        return func

    return decorator


def expect_repeats(count: int):
    """Pin a known-bad path: allow ``count`` repeats, and fail if it stops.

    The counterpart to ``@allow_repeats``, and the difference is the whole point.
    ``@allow_repeats`` is an exemption and it is permanent — the day somebody
    fixes the N+1 it was covering, the decorator stays, the ceiling stays raised,
    and a *new* repeat of any shape in that test is silently inside it. There is
    nothing to tell you the exemption stopped being earned.

    ``@expect_repeats`` is the same allowance plus an assertion that it is still
    needed::

        @expect_repeats(4)
        def test_the_dashboard_still_queries_per_row(self):
            ...

    Fix the N+1 and this test fails, naming the decorator to remove. So a known
    N+1 can be recorded at the call site, where a reader sees it, rather than in
    a generated file — and it cannot rot there.

    **The pin is exact in both directions.** The count is the allowance as well as
    the assertion, so a repeat that gets *deeper* fails too — as an ordinary
    repeated-shape finding, with a message that names this decorator rather than
    telling you to add ``@allow_repeats``. That is deliberate: the decorator is a
    statement about how deep a known N+1 is, and a change in either direction is
    worth a reader's attention.

    **It counts repeats, not shapes**, and that is a real limit rather than an
    oversight: the assertion is "some shape in this test still repeats ``count``
    times", so fixing the N+1 it was written about while introducing a different
    one of the same depth keeps it green. Naming the shape would mean a SQL
    string in a decorator, which is unreadable and breaks on any schema change.
    For shape-level pinning the baseline file is the mechanism — it records the
    SQL — and the two compose: a baselined test may also carry this decorator.

    **It fails in report-only mode too.** Report-only exists so that a *blanket*
    detector does not redden a suite nobody has triaged yet; this is an assertion
    the author wrote by hand about one test, and silencing it would make the
    decorator a synonym for ``@allow_repeats`` in the mode most projects start in
    — which is the exact rot it exists to prevent.
    """

    def decorator(func):
        func._allow_repeats = count
        func._expect_repeats = count
        return func

    return decorator


def report_only() -> bool:
    """Whether findings are collected and printed instead of failing tests."""
    return getattr(settings, "QUERY_GUARD_REPORT_ONLY", True)


def default_threshold() -> int:
    """How many times one shape may appear in a window before it is a finding."""
    return getattr(settings, "QUERY_GUARD_MAX_REPEATS", 1)


def baseline_path() -> Path | None:
    """Where the baseline is read from and written to, or ``None`` if unconfigured.

    **There is deliberately no default**, and that is the one substantive
    difference between this and the application it was ported from. There, the
    module lives in the app and the baseline sits beside it. Here the module is
    installed into the adopter's ``site-packages``, so a path derived from
    ``__file__`` would have the guard writing a generated artifact into their
    virtualenv — outside their repo, unreviewable in a diff, and destroyed by the
    next ``uv sync``. Which is to say: a ratchet that silently stops ratcheting.

    A ``BASE_DIR``-derived default is the other tempting answer and it is also
    wrong: it commits a file to the project root nobody asked for, and
    ``BASE_DIR`` is a convention rather than a guarantee — Django's own
    ``global_settings`` does not define it.

    So the setting is required to use the ratchet at all, and unset means "no
    baseline": every shape judged against the declared threshold, nothing
    excused. That is exactly the behaviour of the guard without this module, so
    turning the ratchet on is opt-in and no existing adopter's run changes.
    """
    configured = getattr(settings, "QUERY_GUARD_BASELINE", None)
    # Falsy rather than `is None`: an empty string is how a project spells
    # "unset" when the value comes from the environment, and `Path("")` is
    # `Path(".")` — a directory, which would fail later as a confusing
    # IsADirectoryError from a write rather than here as "not configured".
    return Path(configured) if configured else None


def updating_baseline() -> bool:
    """Whether this run rewrites the baseline instead of enforcing it.

    An environment variable rather than a setting: regenerating is something you
    do to one invocation from the shell, and a setting would have to be toggled
    in a file that is then easy to commit in the on position.
    """
    return bool(os.environ.get("QUERY_GUARD_UPDATE_BASELINE"))


def baselinable(test_id: str) -> bool:
    """Whether this test id may carry a baseline entry.

    A class defined inside a test method — which is how this library's own
    positive controls plant an N+1 — has ``<locals>`` in its qualified name, so
    its id names the method that built it rather than anything discovery can
    reach. Baselining one is never right and is actively harmful: it silences
    the tests whose whole job is to prove the guard still fires, and they fail
    rather than the guard going quietly blind.

    Excluded on read as well as on write, so a hand-added entry cannot mute a
    positive control either.
    """
    return "<locals>" not in test_id


def judged_against_run_baseline() -> bool:
    """Whether the current test's baseline is the one this run is using."""
    if not _RUN_ACTIVE:
        return True
    return baseline_path() == _RUN_BASELINE_PATH


class UnreadableBaselineEntry(RuntimeError):
    """One entry the parser refuses. Every ``_parse_entry`` refusal raises this.

    It exists so ``carryable_baseline`` can catch *by intent* rather than by base
    class. The distinction that matters is not "legacy versus corrupt" — it is
    **one entry versus the whole file**. An entry the parser cannot read can be
    dropped and re-measured; a file that is not JSON says nothing about any
    individual entry, so there is nothing to carry past, and that raises a plain
    ``RuntimeError`` from ``read_baseline_json`` instead.

    Catching this rather than ``RuntimeError`` keeps a genuine bug inside
    ``_parse_entry`` from being silently counted as "one unreadable entry" on the
    one path whose whole job is to be forgiving.
    """


class LegacyBaselineFormat(UnreadableBaselineEntry):
    """The entry maps a test id straight to a bare integer.

    Its own subclass because it is the one refusal a reader is likely to hit
    without having hand-edited anything — a file generated by an older version of
    this guard, or by the application it was ported from — so it earns a message
    about migration rather than about not hand-editing.

    Nothing branches on the type: ``carryable_baseline`` drops every
    ``UnreadableBaselineEntry`` alike, and ``load_baseline`` refuses them alike.
    The subclass is for the message, not for control flow, and this docstring is
    the only thing standing between that and a future reader assuming otherwise.
    """


def read_baseline_json() -> dict[str, object]:
    """The baseline file as raw JSON. A missing or unconfigured file is empty.

    Split out from ``load_baseline`` so the update path can validate entry by
    entry rather than all-or-nothing. Corruption stays a hard error here: a file
    that is not JSON says nothing about any individual entry.
    """
    path = baseline_path()
    if path is None:
        return {}
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Raised once per test, so say which file and how to rebuild it. A bare
        # decode traceback repeated several hundred times names neither.
        raise RuntimeError(
            f"query guard: {path} is not valid JSON ({exc}). Do not hand-edit it; regenerate with {REGENERATE_COMMAND}."
        ) from exc
    if not isinstance(parsed, dict):
        # Valid JSON, wrong shape — a list or a bare string reaches `.items()` as
        # a bare AttributeError, which names neither the file nor the remedy.
        # Whole-file, so it belongs here with the decode error rather than with
        # the per-entry refusals: there are no entries to carry.
        raise RuntimeError(
            f"query guard: {path} is valid JSON but not an object — it is a "
            f"{type(parsed).__name__}. Do not hand-edit it; regenerate with "
            f"{REGENERATE_COMMAND}."
        )
    return parsed


def _parse_entry(path: Path, test_id: str, entry: object) -> dict[str, int]:
    """One baseline entry, or a refusal naming the entry that is wrong."""
    if isinstance(entry, int) and not isinstance(entry, bool):
        raise LegacyBaselineFormat(
            f"query guard: {path} entry {test_id!r} maps to a bare count. That "
            "format exempted every shape in the test rather than the one that "
            "earned it, which is a hole and not a ratchet. It is refused rather "
            "than read: a bare integer carries no record of which shape earned "
            f"it, so there is nothing to migrate it from. Regenerate with "
            f"{REGENERATE_COMMAND}, which rewrites the file in place."
        )
    if not isinstance(entry, dict):
        raise UnreadableBaselineEntry(
            f"query guard: {path} entry {test_id!r} is not a {{query: count}} "
            f"mapping. Do not hand-edit it; regenerate with {REGENERATE_COMMAND}."
        )
    parsed: dict[str, int] = {}
    for sql, count in entry.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            # `< 1` as well as the type check: a zero or negative count excuses
            # nothing (`max(declared, -5)` is `declared`) *and* can never be
            # reported stale (`observed < -5` is false), so it would sit in the
            # file forever meaning nothing. Only a hand edit can produce one —
            # `write_baseline` drops empty entries — which is the one abuse this
            # design otherwise cannot prevent.
            raise UnreadableBaselineEntry(
                f"query guard: {path} entry {test_id!r} records an invalid count "
                f"{count!r}; an allowance is an integer of at least 1. Do not "
                f"hand-edit it; regenerate with {REGENERATE_COMMAND}."
            )
        parsed[sql] = count
    return parsed


def load_baseline() -> dict[str, dict[str, int]]:
    """Recorded allowance per test id **per query shape**. Missing file is empty.

    Read on every call rather than cached, so a test can point
    ``QUERY_GUARD_BASELINE`` somewhere else with ``override_settings`` and have
    it take effect. The file is small and the read happens once per test.

    Strict: any entry ``_parse_entry`` refuses raises. Forgiving on regeneration,
    strict on enforcement — see ``carryable_baseline``.
    """
    path = baseline_path()
    return {test_id: _parse_entry(path, test_id, entry) for test_id, entry in read_baseline_json().items()}


def carryable_baseline() -> tuple[dict[str, dict[str, int]], int]:
    """Entries an update run may carry, and how many it could not read.

    **Every** entry ``_parse_entry`` refuses is dropped here, not only the legacy
    scalar one, and that breadth is the point: every one of those refusals
    prescribes ``REGENERATE_COMMAND``, and this function is what that command
    reads the baseline through. Catching only ``LegacyBaselineFormat`` would
    leave the other refusals naming a remedy that dies on the same exception.

    **Per entry, not all-or-nothing**, because a file with one bad line in it is
    what a hand-resolved merge conflict on a few hundred lines of generated JSON
    produces. Discarding every valid entry over one bad line would turn a
    conflict into a disarmed ratchet.

    Enforcement is unaffected and stays strict — ``load_baseline`` still raises
    on any of them, so a run reading a bad file is loudly red.

    Nothing is lost on the **full-suite** regeneration the messages prescribe,
    because a full run re-measures every test. A *partial* update run drops every
    unreadable entry it did not exercise, which is exactly the case this
    protects, and is why the count is returned rather than merely logged: the
    caller puts it on the ``wrote`` line, where a reader is actually looking.
    """
    path = baseline_path()
    carried: dict[str, dict[str, int]] = {}
    unreadable = 0
    for test_id, entry in read_baseline_json().items():
        try:
            carried[test_id] = _parse_entry(path, test_id, entry)
        except UnreadableBaselineEntry:
            # The base class, not `LegacyBaselineFormat`: every refusal is the
            # same thing here — one entry that cannot be read, about to be
            # re-measured. And not a bare `RuntimeError`, which would silently
            # count a genuine bug inside `_parse_entry` as one more unreadable
            # entry on the one path meant to be forgiving.
            unreadable += 1
    return carried, unreadable


def unbaselined_shapes(offenders: dict[str, int], recorded: dict[str, int], declared: int) -> dict[str, int]:
    """The shapes that broke their own allowance, keyed by SQL.

    The allowance is looked up **per shape**, so an entry excuses the query that
    earned it and nothing else; a shape the entry does not name falls back to
    ``declared`` and is judged as strictly as it would be in a test with no entry
    at all. A whole-test allowance is a hole rather than a ratchet: a brand-new
    2x repeat of a completely unrelated query lands inside it silently.

    ``max(declared, recorded)`` per shape, so that deleting an entry can only
    tighten, and a stray small recorded count cannot undercut an
    ``@allow_repeats``.
    """
    return {sql: count for sql, count in offenders.items() if count > max(declared, recorded.get(sql, 0))}


def write_baseline(observed: dict[str, dict[str, int]], path: Path) -> int:
    """Record the worst count per test per shape; return the entries written.

    Sorted, indented, newline-terminated: the file's whole value is that a diff
    of it is readable, so a review can see a ratchet tighten. Naming the shape
    makes it readable a second way — the diff shows *which* query a fix removed,
    not only that a number went down.

    The count is returned rather than recomputed by the caller because the
    filtering here is what decides it: reporting ``len(observed)`` overstates the
    file by every entry this function drops.
    """
    entries = {test_id: shapes for test_id, shapes in observed.items() if shapes and baselinable(test_id)}
    # `sort_keys=True` sorts the test ids *and* each entry's shapes, which is the
    # whole ordering guarantee — sorting the dicts first as well would be a
    # second mechanism for one property.
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    return len(entries)


def merge_baseline(
    existing: dict[str, dict[str, int]], observed: dict[str, dict[str, int]]
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Entries to write, plus the ids carried over from a partial run.

    A regeneration that ran only part of the suite has measured *nothing* about
    the rest, so those entries are carried rather than deleted. Deleting them
    reads in review exactly like the ratchet tightening — which is the file's
    whole job — while actually disarming every test the run did not reach.

    An observed test is *not* carried: dropping its entry is how the ratchet
    tightens, and that is the one deletion the run has evidence for.
    """
    carried = sorted(test_id for test_id in existing if test_id not in observed)
    merged = dict(observed)
    for test_id in carried:
        merged[test_id] = existing[test_id]
    return merged, carried


def stale_entries(baseline: dict[str, dict[str, int]], observed: dict[str, dict[str, int]]) -> list[str]:
    """Baselined tests that ran and carry at least one shape they no longer earn.

    Only tests that actually ran are judged, so a partial run does not report the
    rest of the suite as stale.

    Per shape, which makes this strictly more useful than a whole-test check: a
    test whose recorded 5x shape is now 2x while a second shape is unchanged is
    invisible to a check on the test's *worst* count. A shape that vanished
    entirely reads as observed 0.
    """
    return sorted(
        test_id
        for test_id, shapes in baseline.items()
        if test_id in observed and any(observed[test_id].get(sql, 0) < recorded for sql, recorded in shapes.items())
    )


def is_select(sql: str) -> bool:
    """Whether this statement is a read, and so a candidate for an N+1.

    Reads only: a repeated ``INSERT`` shape is an ordinary bulk write, and
    counting those makes routine fixture setup look like an N+1.

    A prefix test, so it does not recognise a read that does not start with the
    word: ``WITH ... SELECT`` (what ``django-cte`` and some ``RawSQL`` emit) and
    ``EXPLAIN`` are both missed. Widening it is a behaviour change for existing
    adopters — a previously-invisible shape starts failing tests — so it belongs
    behind a setting rather than in a patch release.
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

    def worst_count(self) -> int:
        """The highest number of times any one shape appeared in any one window.

        0 for a collector that saw no reads at all, 1 for one that saw only
        distinct ones. What ``@expect_repeats`` is asserting against, which is why
        it is a method here rather than ``max(repeated(0).values())`` at the call
        site: ``repeated(0)`` reads as "shapes over a threshold of zero", and a
        reader has to work out that this means all of them.
        """
        return max(
            (count for window in self.windows for count in Counter(window).values()),
            default=0,
        )


@contextmanager
def new_window():
    """Bracket a unit of work Django does not signal.

    Celery task bodies, management commands and service functions called
    directly from a test emit no ``request_started``, so without this the whole
    test body is one window and a loop over two of them reads as an N+1 of
    every shape they share.

    Banks on both entry and exit, exactly as the request signals do, so the work
    inside is counted on its own and is not charged to whatever ran before or
    after it.

    Safe to call with no guard active — it is then a no-op — so a helper shared
    between a test and a management command can use it unconditionally. That is
    not an invitation to import this module from production code: it pulls
    ``django.test.runner`` and registers an ``atexit`` hook at import.
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


def format_finding(test_id: str, offenders: dict[str, int], allowances: dict[str, int] | None = None) -> str:
    """Render one test's offending shapes as an indented block.

    Each line carries the allowance *that shape* was judged against, from a
    mapping rather than one number, because with a baseline those differ within a
    single finding: a shape the baseline names is judged against its recorded
    count while its neighbour falls back to the declared threshold. A single
    number in the header is then wrong for at least one line whenever the failing
    set is mixed, and a reader seeing ``2x`` under ``threshold 5`` reasonably
    concludes the guard is broken.

    ``allowances`` is optional because the report-only path has none to show: it
    collects before any baseline is consulted.
    """
    lines = []
    for sql, count in sorted(offenders.items()):
        allowed = (allowances or {}).get(sql)
        prefix = f"  {count}x" if allowed is None else f"  {count}x (allowed {allowed})"
        lines.append(f"{prefix}  {sql}")
    return f"{test_id}\n" + "\n".join(lines)


def format_report(findings: list[tuple[str, dict[str, int]]]) -> str:
    """Render the whole run's findings, or a clean bill of health."""
    if not findings:
        return "query guard: no repeated query shapes detected"
    rule = "=" * 78
    blocks = [format_finding(test_id, offenders) for test_id, offenders in findings]
    # The footer has to describe the run that produced it. An update run collects
    # through this same list, and the report-only wording was then wrong three ways
    # at once: it is not report-only, it says "fix these" about findings the run
    # just *recorded*, and it prescribes a setting already in force. These messages
    # are the whole interface to a mechanism nobody can see working.
    if updating_baseline():
        footer = (
            "Recorded in the baseline, not failed. Review the diff and commit the "
            "file; each entry raises the allowance for the shape it names and no "
            "further, so a new repeat of anything else still fails."
        )
    else:
        footer = (
            "Report-only mode: nothing failed. Fix these, or mark the deliberate ones "
            "with @allow_repeats(n), then set QUERY_GUARD_REPORT_ONLY = False to make "
            "this a gate."
        )
    return (
        f"\n{rule}\nquery guard: {len(findings)} test(s) with repeated query shapes\n"
        f"{rule}\n" + "\n".join(blocks) + f"\n{rule}\n" + footer
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
        # A set, not a bare string: `databases` is normally a frozenset by the
        # time setUpClass has run, but it is spelled `"__all__"` in source and
        # `"default" not in "__all__"` is a substring test that answers True.
        declared = getattr(test, "databases", {"default"})
        if isinstance(declared, str):
            declared = {declared}
        if declared != {"__all__"} and manager.db not in declared:
            return
        for content_type in manager.all():
            manager._add_to_cache(manager.db, content_type)
    except _ForbiddenQuery:
        # An ancestor `SimpleTestCase` installed the "no queries here" patch and
        # it is still in force, so this test cannot be warmed however its own
        # `databases` reads. Ordinary configuration, not a surprise: reporting it
        # would make the loud `except` below mean "this ran normally", which is
        # exactly what it must not mean.
        return
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


def warn_update_without_runner() -> None:
    """Say, once, that ``QUERY_GUARD_UPDATE_BASELINE`` is set with no runner.

    Only ``QueryGuardRunner`` writes the baseline. Under ``QueryGuardMixin`` the
    variable can therefore neither regenerate anything nor be refused by
    ``_require_writable_baseline``, and enforcement still steps aside for it — so
    without this the run is green, the file is absent, and nothing says why. That
    is the "green that reads as a checked green" the rest of this module spends
    three refusals preventing.

    A warning rather than a refusal, because the variable is a legitimate thing to
    have exported while running a mixin-guarded module on purpose; what is not
    legitimate is doing it silently.
    """
    global _UPDATE_WITHOUT_RUNNER_REPORTED
    if _UPDATE_WITHOUT_RUNNER_REPORTED:
        return
    _UPDATE_WITHOUT_RUNNER_REPORTED = True
    print(
        "query guard: QUERY_GUARD_UPDATE_BASELINE is set but no QueryGuardRunner is "
        "active, so nothing will be written and nothing is being enforced. Only the "
        "runner regenerates the baseline — set "
        'TEST_RUNNER = "abi_django_utils.queryguard.QueryGuardRunner" (or pass '
        "--testrunner=...) for this run, or unset the variable.",
        file=sys.stderr,
        flush=True,
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
    """The repeat threshold for this test, honouring ``@allow_repeats``.

    Read off the **class**, not the instance, because Django replaces the
    instance attribute for a coroutine test: ``SimpleTestCase._setup_and_call``
    does ``setattr(self, name, async_to_sync(testMethod))``, and that
    ``AsyncToSync`` wrapper does not carry ``_allow_repeats``. Reading the
    instance therefore silently ignored the decorator on every ``async def
    test_*``, and the failure it produced told the author to add the decorator
    they had already added.

    The class attribute is the undecorated function in both cases, so it answers
    for sync and async alike — a mutation round confirmed the instance read it
    replaced was contributing nothing.
    """
    name = getattr(test, "_testMethodName", "")
    method = getattr(type(test), name, None)
    return getattr(method, "_allow_repeats", default_threshold())


def expected_repeats_for(test) -> int | None:
    """The ``@expect_repeats`` count for this test, or ``None``.

    Read off the class for the same reason ``threshold_for`` is — Django replaces
    the instance attribute with an ``AsyncToSync`` wrapper for a coroutine test,
    and that wrapper carries neither marker.
    """
    name = getattr(test, "_testMethodName", "")
    method = getattr(type(test), name, None)
    return getattr(method, "_expect_repeats", None)


def check(test, collector: QueryCollector, *, completed: bool = True) -> None:
    """Record or raise, depending on mode. Called once per test.

    ``completed`` is False when the test body raised — a failure, an error, or
    an in-body ``self.skipTest()``. Such a run measured only the queries made
    before it stopped, so it is an *under*-count, never an over-count.

    That asymmetry drives three decisions here.

    Enforcing mode stays quiet: adding a second failure on top of the real one
    buries the cause. Report-only mode still records it, because the report is
    informational and cannot make a failing test worse — and dropping it means a
    test that N+1s and *then* fails for an unrelated reason contributes nothing to
    the triage list the rollout is built on, so the finding reappears only once
    the unrelated failure is fixed.

    The third is the baseline's, and it is the one that would be a silent data
    loss rather than a missing message: an incomplete run is left out of
    ``_OBSERVED`` entirely. Recording an under-count there is wrong in both
    directions — the stale report calls a still-earned entry unnecessary, and an
    update run **deletes** it outright, since ``merge_baseline`` only carries
    entries for tests it did not observe. Leaving the test out is what makes "did
    not run" and "ran and repeated nothing" different states.

    Decorator skips (``@skipIf``, ``@unittest.skip``) never reach here at all,
    because ``_callTestMethod`` is not called for them.
    """
    test_id = test.id()
    declared = threshold_for(test)

    # What this test repeats at all, judged against its declared threshold.
    # Recorded before the baseline is consulted, because the baseline is written
    # from this number and must not be derived from itself.
    offenders = collector.repeated(declared)
    on_run_baseline = judged_against_run_baseline()
    if on_run_baseline and completed:
        global _OBSERVATIONS
        _OBSERVED[test_id] = dict(offenders)
        _OBSERVATIONS += 1

    # Before the offenders path, and before any mode check. A test the author
    # pinned with @expect_repeats has to fail when the repeat is *gone*, which is
    # precisely the case where `offenders` is empty and every branch below
    # returns. Skipped on an incomplete run for the usual reason: the count is an
    # under-count, so "it stopped repeating" is not a thing this run can know.
    # `_RUN_ACTIVE` above is load-bearing and not belt-and-braces. Only the runner
    # writes the file, so under `QueryGuardMixin` — no `run_suite`, no
    # `settle_baseline` — an update run would have collected instead of enforcing,
    # written nothing, and said nothing: a green suite, no file, no diagnostic. The
    # runner refuses this exact situation outright, and an env var left exported in
    # a shell is precisely how it gets reached by accident.
    if updating_baseline() and not _RUN_ACTIVE:
        warn_update_without_runner()

    expected = expected_repeats_for(test)
    if expected is not None and completed:
        worst = collector.worst_count()
        if worst < expected:
            # No `return` after this: `fail_expectation` calls `test.fail()`, which
            # raises unconditionally, so one would be unreachable — and an
            # unreachable line inside a guard reads as a branch somebody forgot to
            # cover. `ExpectRepeatsTests` pins the raise, so the day that stops
            # being true is a red test rather than a silent fall-through into the
            # offenders path below.
            fail_expectation(test, test_id, expected, worst)

    if not offenders:
        return

    # An update run collects instead of enforcing — but only for tests that can
    # be baselined, and only against the run's own file. Without the first
    # condition the regeneration command is itself red: this library's positive
    # controls plant an N+1 and assert the inner test fails, and collecting
    # instead of failing makes every one of them report a pass.
    collecting = report_only() or (updating_baseline() and _RUN_ACTIVE and on_run_baseline and baselinable(test_id))
    if collecting:
        _FINDINGS.append((test_id, offenders))
        return

    if not completed:
        return

    # A test that cannot be baselined is judged as if the file were empty, so a
    # hand-added entry cannot mute a positive control.
    recorded = load_baseline().get(test_id, {}) if baselinable(test_id) else {}
    unbaselined = unbaselined_shapes(offenders, recorded, declared)
    if not unbaselined:
        return
    allowances = {sql: max(declared, recorded.get(sql, 0)) for sql in unbaselined}
    fail_with(test, test_id, allowances, unbaselined)


def fail_with(test, test_id: str, allowances: dict[str, int], offenders: dict[str, int]) -> None:
    """Fail ``test``, naming the allowance each shape broke and what to do.

    The advice is conditional on what the test already carries, because advice that
    prescribes something already present is worse than none: it reads as the guard
    not having noticed, and the reader goes looking for the bug in their own test.
    """
    expected = expected_repeats_for(test)
    if expected is not None:
        # Already pinned. `@expect_repeats(n)` sets the allowance *and* asserts the
        # repeat is still n deep, so it pins the count from both sides — which means
        # a repeat that got DEEPER arrives here, and telling that author to add
        # `@allow_repeats` names a decorator they have already written one better
        # than. The thing to change is the number.
        advice = (
            f"\n\nThis test is decorated @expect_repeats({expected}), which pins the "
            "repeat at exactly that depth. It got deeper, so either the regression is "
            "real — fix it — or the new depth is correct and the decorator's number "
            "should follow it."
        )
    else:
        advice = (
            "\n\nIf the repetition is deliberate, decorate the test with "
            "@allow_repeats(n), or @expect_repeats(n) to pin a known N+1 so it fails "
            "when somebody fixes it. If it is a real N+1, fix it — select_related or "
            "prefetch_related is usually the answer. If the repeats are separate "
            "units of work, bracket each one with new_window()."
        )
    path = baseline_path()
    # Only mentioned when there is one. Telling a project with no baseline not to
    # hand-edit a file it does not have is noise in the middle of the one message
    # that has to be actionable.
    baseline_note = f" Do not hand-edit {path.name}; regenerate it with {REGENERATE_COMMAND}." if path else ""
    test.fail(
        "repeated query shape (likely N+1):\n" + format_finding(test_id, offenders, allowances) + advice + baseline_note
    )


def fail_expectation(test, test_id: str, expected: int, worst: int) -> None:
    """Fail ``test`` because the N+1 it pins is gone. The good failure."""
    test.fail(
        f"{test_id} is decorated @expect_repeats({expected}), but no query shape "
        f"repeated more than {worst} time(s) in any window.\n\n"
        "If you fixed the N+1 this test was pinning: remove the decorator. That "
        "is what it is for — an @allow_repeats would have stayed, kept the "
        "ceiling raised, and let a new repeat in silently.\n"
        "If the test stopped exercising the path instead, that is the real "
        "regression, and the decorator just told you about it."
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

    #: Baselined tests this run proved no longer need their entry, filled in by
    #: ``settle_baseline``. A class attribute as well as an instance one so
    #: ``suite_result`` is safe on a runner whose ``run_suite`` never got that far.
    #: Only ever *rebound* (``self.x = [...]``), never mutated in place — an
    #: ``.append()`` here would write through to this shared class-level list and
    #: become process-global state every runner instance sees.
    stale_baseline_entries: list[str] = []

    #: Whether the run that produced those entries was report-only. Captured for
    #: the same reason the path is: ``suite_result`` runs after ``run_suite`` has
    #: returned, so a re-read can answer about a different mode than the one the
    #: verdict was computed under — and ``settle_baseline`` and ``suite_result``
    #: then disagree about whether a stale entry is a note or a gate.
    stale_baseline_report_only: bool = True

    #: The file those entries were measured against, captured at the same moment.
    #: ``suite_result`` must not re-read ``baseline_path()``: it runs after
    #: ``run_suite`` has returned, so the setting can have changed — and when it
    #: has changed to *unset*, the re-read is ``None`` and naming the file is an
    #: ``AttributeError`` out of the one branch whose job is to report. Naming the
    #: file the verdict came from is also just more honest than naming whichever
    #: file the setting points at by the time the summary prints.
    stale_baseline_path: Path | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Here rather than in `run_suite`, which Django reaches only after
        # `setup_databases` has cloned one test database per worker. The verdict
        # is the same either way; refusing before the clones saves the minutes.
        if getattr(self, "parallel", 0) > 1:
            self._require_parallel_support()

    def suite_result(self, suite, result, **kwargs):
        """Django's failure count, plus one if the baseline is out of date.

        This is what makes the stale report a *gate* rather than a line in a log
        nobody reads. Without it a fix that tightens the ratchet can land while
        the file still records the old number, and the next PR to regenerate then
        ships deletions nobody in it caused.

        Counted here rather than raised from ``settle_baseline``, and the
        difference matters: ``settle_baseline`` runs in ``run_suite``'s
        ``finally``, so raising there would pre-empt the real test summary and a
        run with both a genuine failure and a stale entry would report only the
        stale one. Adding to the count composes — both are reported, and the exit
        code is non-zero either way.

        Report-only mode keeps the old behaviour, because a stale entry is a
        control that is *tighter* than recorded and someone mid-fix should not be
        blocked by it.
        """
        failures = super().suite_result(suite, result, **kwargs)
        if not self.stale_baseline_entries or self.stale_baseline_report_only:
            return failures
        named = self.stale_baseline_path.name if self.stale_baseline_path else "the baseline"
        # Phrased against `result`, not asserted. unittest prints `OK` when the
        # only thing wrong is the file, and a red exit under a green summary is
        # what gets misdiagnosed — but on a run that also has real failures,
        # claiming "the tests passed" would be a plain lie printed directly under
        # `FAILED`.
        clean = not (result.failures or result.errors or result.unexpectedSuccesses)
        preamble = (
            "The tests themselves passed — what is out of date is "
            if clean
            else "Separately from the failures above, what is also out of date is "
        )
        print(
            "\nquery guard: FAILING THE RUN on the stale entries above. "
            + preamble
            + f"{named}. Regenerate it and commit the result; a stale "
            "entry makes every later diff of that file carry deletions nobody in "
            "that PR caused.",
            file=sys.stderr,
            flush=True,
        )
        return failures + 1

    def run_suite(self, suite, **kwargs):
        global _RUN_ACTIVE, _RUN_BASELINE_PATH
        if updating_baseline():
            self._require_writable_baseline()
        # Saved and restored, not nulled. A test may build a nested runner of its
        # own, and nulling on the way out leaves every *later* test judged by
        # `judged_against_run_baseline()` as if it were using the outer run's
        # baseline, which is the scoping bug this guards against.
        outer_active, outer_path = _RUN_ACTIVE, _RUN_BASELINE_PATH
        _RUN_ACTIVE, _RUN_BASELINE_PATH = True, baseline_path()
        # Per *invocation*, not per process: `_OBSERVED` accumulates across the
        # whole process, and a test may build a nested runner of its own, so
        # "observed nothing" has to be a delta rather than an emptiness check.
        observed_before = observation_count()
        original = install()
        try:
            return super().run_suite(suite, **kwargs)
        finally:
            unittest.TestCase._callTestMethod = original
            # Restored BEFORE settling, not after. `settle_baseline` can raise
            # SystemExit — it does so on an empty update run, which is exactly
            # what a nested runner built by a test produces — and a restore
            # placed after it is then skipped, leaving `_RUN_BASELINE_PATH`
            # pointing at the nested run's file for every later test in the outer
            # run. Nothing below reads it, so moving it up is free.
            _RUN_ACTIVE, _RUN_BASELINE_PATH = outer_active, outer_path
            self._report()
            self.settle_baseline(observation_count() - observed_before)

    def settle_baseline(self, observed: int) -> None:
        """Rewrite the baseline, or name the entries no longer earning it.

        Two different jobs on purpose. Writing happens only when asked for
        explicitly. Staleness is reported on every enforcing run and recorded on
        the runner so ``suite_result`` can fail it; in report-only mode it stays a
        note, because someone mid-fix should not be blocked by a control that is
        tighter than recorded.
        """
        self.stale_baseline_entries = []
        self.stale_baseline_path = None
        self.stale_baseline_report_only = report_only()
        path = baseline_path()
        if path is None:
            # No baseline configured: nothing to write and nothing that can be
            # stale. `run_suite` has already refused an update run by here.
            return
        if updating_baseline():
            self._write_baseline(path, observed)
            return
        # An unreadable entry has no per-shape counts to judge staleness against,
        # and a run reading one is already loudly red from `check()`. Raising a
        # second time here only buries that under a traceback from the summary.
        # The entries that *are* readable are still judged.
        existing, unreadable = carryable_baseline()
        if unreadable:
            print(
                f"query guard: {unreadable} unreadable entr"
                f"{'y is' if unreadable == 1 else 'ies are'} in {path} and cannot "
                f"be enforced. Regenerate with the FULL suite: {REGENERATE_COMMAND}",
                file=sys.stderr,
                flush=True,
            )
        if getattr(self, "parallel", 0) > 1:
            # `_OBSERVED` lives in the worker processes and cannot be gathered
            # back — the same mechanism that makes an update run refuse under
            # `--parallel`. Here the consequence is a false green rather than a
            # bad write, so it says so instead of refusing: a gate that silently
            # does nothing is worse than no gate.
            print(
                "query guard: the stale-baseline check is INERT under --parallel "
                "(findings live in the worker processes). Run serially before "
                "trusting a green.",
                file=sys.stderr,
                flush=True,
            )
            return
        stale = stale_entries(existing, _OBSERVED)
        if not stale:
            return
        self.stale_baseline_entries = stale
        self.stale_baseline_path = path
        print(
            f"\nquery guard: {len(stale)} baseline entr"
            f"{'y is' if len(stale) == 1 else 'ies are'} no longer needed — "
            f"regenerate with {REGENERATE_COMMAND}:\n  " + "\n  ".join(stale),
            file=sys.stderr,
            flush=True,
        )

    def _write_baseline(self, path: Path, observed: int) -> None:
        """Merge this run's observations into the file and say what happened."""
        # A run that bracketed no tests has nothing to say about the file. Writing
        # anyway blanks it, and a blanked baseline is a *passing* suite until the
        # next real regeneration — the quietest possible way for this mechanism to
        # stop working.
        #
        # `SystemExit`, where `_require_writable_baseline`'s sibling refusals raise
        # `ImproperlyConfigured`, and the split is deliberate. Those are settings
        # errors, raised before the suite, where a traceback naming the class is the
        # Django-idiomatic thing and costs nothing. This one fires from
        # `run_suite`'s `finally`, after the whole suite has reported, where a
        # traceback would bury the output the reader is actually there for.
        # `SystemExit(msg)` prints the message alone.
        if not observed and path.exists():
            raise SystemExit(
                f"query guard: refusing to rewrite {path} from a run that observed "
                f"no tests. Regenerate with the full suite: {REGENERATE_COMMAND}"
            )
        existing, unreadable = carryable_baseline()
        merged, carried = merge_baseline(existing, _OBSERVED)
        written = write_baseline(merged, path)
        note = ""
        if unreadable:
            # On the `wrote` line, not in a note above it. `wrote (54 entries)`
            # after a partial migration reads exactly like an ordinary ratchet
            # tightening, so the number a reader looks at has to carry the number
            # they would regret not seeing.
            note += (
                f"; DISCARDED {unreadable} unreadable entr"
                f"{'y' if unreadable == 1 else 'ies'} — a legacy scalar count, or "
                "a hand edit — if this was not a FULL-suite run, those tests are "
                "now unbaselined"
            )
        if carried:
            # `+=`, not `=`. A mixed file produces both clauses, and an assignment
            # here silently drops the discard warning in exactly the case where it
            # matters most.
            note += (
                f"; carried {len(carried)} entr"
                f"{'y' if len(carried) == 1 else 'ies'} for tests this run did not "
                "exercise — regenerate with the full suite to drop them"
            )
        print(
            f"query guard: wrote {path} ({written} entries){note}",
            file=sys.stderr,
            flush=True,
        )

    def _require_writable_baseline(self) -> None:
        """Refuse an update run that has nowhere to write, or cannot write safely.

        Both refusals are about the same thing — an update run that *looks* like
        it worked, and leaves a file nobody has reason to re-examine.

        The ``--parallel`` one is the sharper of the two: ``_OBSERVED`` is
        per-process and nothing gathers the workers' copies back, so the file
        would record whichever worker happened to write last and silently drop the
        rest. Unlike the enforcement side, where the same mechanism costs a false
        green that a serial run then corrects, this one *writes* the loss to disk.
        """
        if baseline_path() is None:
            raise ImproperlyConfigured(
                "query guard: QUERY_GUARD_UPDATE_BASELINE is set but "
                "QUERY_GUARD_BASELINE is not, so there is nowhere to write. Point "
                "it at a path inside your repository — the file is meant to be "
                "committed and read in a diff."
            )
        parent = baseline_path().parent
        if not parent.is_dir():
            # Here rather than at the `write_text` in `run_suite`'s `finally`, which
            # is reached only after every test has run: a bare FileNotFoundError
            # after a full suite is the cost the `--parallel` check was moved into
            # `__init__` to avoid. Same verdict, minutes cheaper. Not an access
            # check as well — a directory that exists but refuses a write is a
            # permissions problem whose own error names the path.
            raise ImproperlyConfigured(
                f"query guard: QUERY_GUARD_BASELINE points into {parent}, which is "
                "not a directory, so the baseline cannot be written. Point it at a "
                "path inside your repository."
            )
        if getattr(self, "parallel", 0) > 1:
            raise ImproperlyConfigured(
                "query guard: refusing to write a baseline under --parallel. "
                "Findings are collected per worker process and cannot be gathered "
                "back, so the file would record whichever worker wrote last and "
                "silently drop the rest. Re-run serially (--parallel=1)."
            )

    def _report(self) -> None:
        """Emit the findings, or say the guard ran and found none.

        The clean case needs saying. ``emit_report()`` returns early with no
        findings, so without this a run with the guard installed and a run with
        ``TEST_RUNNER`` misspelled print exactly the same thing — and the whole
        point of a blanket detector is that nobody is annotating anything, so
        there is no other evidence it was active.
        """
        if _FINDINGS:
            emit_report()
            return
        if not report_only():
            return
        print("query guard: active, no repeated query shapes detected", file=sys.stderr, flush=True)

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
