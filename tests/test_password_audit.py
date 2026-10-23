"""Tests for the admin-password audit and the preventive validator defaults.

Two assertions here are the ones that matter, and both are easy to write in a
form that cannot fail:

* **"the output contains no password material."** An absence assertion passes
  just as happily when the account was never reported at all, so every such
  test below carries a *reachability control* -- it also asserts the account's
  label IS present -- and a *checker control* proving ``mentions()`` returns
  True on a string that really does contain the needle. Without both, a
  command that printed nothing would score as secure.
* **"CommonPasswordValidator is wired up."** Asserted through
  ``validate_password`` rather than by looking for the dotted path in the
  settings list, because a validator named in settings and never reached is
  indistinguishable from a working one.

These tests use the project's real hasher (PBKDF2 at 1.2M iterations, ~190 ms
a check) rather than overriding ``PASSWORD_HASHERS`` to something fast. The
subject under test is "does hash comparison find this password", so a stub
hasher would be testing the stub. Candidate lists are kept to a handful of
entries to pay for that honestly.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import logging
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError, OutputWrapper
from django.test import SimpleTestCase, TestCase, override_settings

from abi_django_utils import defaults, password_audit

#: Distinctive enough that finding it in command output cannot be a
#: coincidence, and short enough to cost one hash per check.
SENTINEL_WEAK_PASSWORD = "zz-sentinel-weak-pw-9471"

SHARED_VALIDATORS = defaults.django_defaults()["AUTH_PASSWORD_VALIDATORS"]

MD5_HASHER = "django.contrib.auth.hashers.MD5PasswordHasher"
PBKDF2_HASHER = "django.contrib.auth.hashers.PBKDF2PasswordHasher"


class _RecordingHandler(logging.Handler):
    """Keeps every record so the leak test can grep what was logged."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _root_logger_capture(handler: logging.Handler):
    """Attach ``handler`` to the root logger at DEBUG for the duration."""
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def tmpdir() -> tempfile.TemporaryDirectory:
    """A directory for candidate-list fixtures, torn down with the test."""
    return tempfile.TemporaryDirectory()


def mentions(haystack: str, needle: str) -> bool:
    """Does ``haystack`` contain ``needle``, ignoring case?

    Case-insensitive on purpose: a command that upper-cased a password before
    printing it would still have disclosed it.
    """
    return needle.lower() in haystack.lower()


def write_list(tmp_path: Path, *candidates: str, gzipped: bool = False) -> Path:
    """Write a candidate list to disk and return its path."""
    suffix = ".txt.gz" if gzipped else ".txt"
    path = tmp_path / f"candidates{suffix}"
    body = "\n".join(candidates) + "\n"
    if gzipped:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


class MentionsHelperTests(SimpleTestCase):
    """The checker control. Guards every absence assertion in this file."""

    def test_finds_a_planted_needle(self):
        assert mentions(f"prefix {SENTINEL_WEAK_PASSWORD} suffix", SENTINEL_WEAK_PASSWORD)

    def test_finds_a_planted_needle_whatever_its_case(self):
        assert mentions(f"prefix {SENTINEL_WEAK_PASSWORD.upper()} suffix", SENTINEL_WEAK_PASSWORD)

    def test_absent_needle_is_absent(self):
        assert not mentions("nothing to see here", SENTINEL_WEAK_PASSWORD)


class PasswordListTests(SimpleTestCase):
    """Reading candidate lists."""

    def test_default_path_is_djangos_gzipped_list(self):
        path = password_audit.default_password_list_path()
        assert path.name == "common-passwords.txt.gz"
        assert path.exists()

    def test_default_list_is_ordered_most_common_first(self):
        """A ``--limit`` is only a coverage dial if the list is frequency-ordered.

        Pinned against the first three entries of Django's own file. If
        upstream ever re-sorts it alphabetically, ``DEFAULT_CANDIDATE_LIMIT``
        silently becomes an arbitrary slice and this test is the warning.
        """
        top = password_audit.load_candidates(limit=3)
        assert top == ["123456", "123456789", "qwerty"]

    def test_limit_none_reads_the_whole_file(self):
        everything = password_audit.load_candidates(limit=None)
        assert len(everything) > 19_000
        assert len(everything) > len(password_audit.load_candidates(limit=10))

    def test_default_limit_matches_the_documented_runtime(self):
        """The default is quoted as "~3 min per account" in three places.

        At the measured ~190 ms a check, 1,000 candidates is 3.2 minutes. The
        README, the module docstring and the command's ``--help`` all state
        that figure, so a change to the constant makes three documents wrong
        and nothing else would notice.
        """
        assert password_audit.DEFAULT_CANDIDATE_LIMIT == 1000

    def test_limit_caps_the_result(self):
        assert len(password_audit.load_candidates(limit=7)) == 7

    def test_reads_a_plain_text_file(self):
        tmp_path = Path(self.enterContext(tmpdir()))
        path = write_list(tmp_path, "alpha", "beta")
        assert password_audit.load_candidates(path) == ["alpha", "beta"]

    def test_reads_a_gzipped_file(self):
        tmp_path = Path(self.enterContext(tmpdir()))
        path = write_list(tmp_path, "alpha", "beta", gzipped=True)
        assert password_audit.load_candidates(path) == ["alpha", "beta"]

    def test_skips_blank_lines_and_strips_whitespace(self):
        tmp_path = Path(self.enterContext(tmpdir()))
        path = tmp_path / "candidates.txt"
        path.write_text("alpha\n\n  beta  \n\n", encoding="utf-8")
        assert password_audit.load_candidates(path) == ["alpha", "beta"]

    def test_decompresses_a_gzip_whatever_it_is_named(self):
        """Compression is detected by trying, not by the filename.

        ``--password-file list.txt.gzip`` is not unusual. Opened as text with
        errors suppressed, a gzip stream decodes into junk candidates and the
        audit reports a confident clean run against garbage.
        """
        tmp_path = Path(self.enterContext(tmpdir()))
        misnamed = tmp_path / "candidates.txt.gzip"
        with gzip.open(misnamed, "wt", encoding="utf-8") as handle:
            handle.write("alpha\nbeta\n")
        assert password_audit.load_candidates(misnamed) == ["alpha", "beta"]

    def test_a_plain_file_named_gz_is_still_read(self):
        """Reachability control for the test above, in the other direction."""
        tmp_path = Path(self.enterContext(tmpdir()))
        misnamed = tmp_path / "candidates.gz"
        misnamed.write_text("alpha\nbeta\n", encoding="utf-8")
        assert password_audit.load_candidates(misnamed) == ["alpha", "beta"]

    def test_a_file_that_is_not_text_raises_rather_than_yielding_junk(self):
        """ "Not a password list" must not round down to "some candidates"."""
        tmp_path = Path(self.enterContext(tmpdir()))
        binary = tmp_path / "candidates.txt"
        binary.write_bytes(b"\xff\xfe\x00\x01not utf-8 at all \x80\x81")
        with pytest.raises(UnicodeDecodeError):
            password_audit.load_candidates(binary)

    def test_a_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            password_audit.load_candidates(Path("/nonexistent/candidates.txt"))

    def test_accepts_a_string_path(self):
        """``call_command`` hands options through without running ``type=``."""
        tmp_path = Path(self.enterContext(tmpdir()))
        path = write_list(tmp_path, "alpha")
        assert password_audit.load_candidates(str(path)) == ["alpha"]


class EstimateTests(SimpleTestCase):
    """Runtime estimation."""

    def test_estimate_multiplies_accounts_by_candidates_by_cost(self):
        assert password_audit.estimate_seconds(accounts=3, candidates=1000, seconds_per_check=0.19) == pytest.approx(
            570.0
        )

    def test_seconds_per_check_tracks_the_hash_it_is_given(self):
        """A well-chosen constant passes any single-sample bound. A ratio does not.

        An earlier version of this test compared the measurement against one
        timed here and allowed 4x either way -- which ``return 0.19`` survives,
        because 0.19 s *is* this machine's PBKDF2 cost. MD5 costs ~0.01 ms and
        PBKDF2 at 1.2M iterations ~190 ms, so a function that measures reports
        a four-figure ratio and a constant reports 1.

        Both hashes are verified under one ``PASSWORD_HASHERS`` setting, which
        also demonstrates the claim in ``seconds_per_check``'s docstring: cost
        follows the parameters baked into the stored hash, not the project's
        current preferred hasher.
        """
        with override_settings(PASSWORD_HASHERS=[MD5_HASHER, PBKDF2_HASHER]):
            cheap_hash = make_password("x")
            expensive_hash = make_password("x", hasher="pbkdf2_sha256")
            assert cheap_hash.startswith("md5$")

            cheap = password_audit.seconds_per_check(cheap_hash)
            expensive = password_audit.seconds_per_check(expensive_hash)

        assert cheap > 0
        assert expensive > cheap * 100


class AdminAccountSelectionTests(TestCase):
    """Which accounts the audit looks at."""

    def test_superuser_is_included(self):
        user = User.objects.create_superuser("root", "root@example.com", "pw")
        assert list(password_audit.admin_accounts()) == [user]

    def test_staff_without_superuser_is_included(self):
        user = User.objects.create_user("editor", password="pw", is_staff=True)
        assert list(password_audit.admin_accounts()) == [user]

    def test_ordinary_user_is_excluded(self):
        User.objects.create_user("reader", password="pw")
        assert list(password_audit.admin_accounts()) == []


class WeakAccountLabelTests(TestCase):
    """The audit core, exercised without the command wrapper."""

    def test_reports_the_account_whose_password_is_in_the_list(self):
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        labels = password_audit.weak_account_labels(password_audit.admin_accounts(), [SENTINEL_WEAK_PASSWORD])
        assert labels == [f"{User.objects.get(username='root').pk}:root"]

    def test_a_match_anywhere_in_the_list_is_enough(self):
        """One hit among misses must report.

        Every other weak-account test here passes a single-entry candidate
        list, and over one entry ``any`` and ``all`` agree -- so without this
        case the quantifier in ``password_is_common`` is untested.
        """
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        labels = password_audit.weak_account_labels(
            password_audit.admin_accounts(),
            ["alpha", SENTINEL_WEAK_PASSWORD, "beta"],
        )
        assert labels == [f"{User.objects.get(username='root').pk}:root"]

    def test_says_nothing_about_an_account_outside_the_list(self):
        User.objects.create_superuser("root", "root@example.com", "correct-horse-battery-staple-7731")
        labels = password_audit.weak_account_labels(password_audit.admin_accounts(), [SENTINEL_WEAK_PASSWORD])
        assert labels == []

    def test_unusable_password_is_not_hashed_against_at_all(self):
        """The skip is a cost guard, so cost is what has to be asserted.

        An unusable password is ``!``-prefixed and can never equal any hash,
        so dropping the ``is_password_usable`` check would still return ``[]``
        -- the result is identical and only the thousand wasted hashes differ.
        Asserting ``check_password`` is never reached is the only assertion
        that can fail if the guard goes away.
        """
        user = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        user.set_unusable_password()
        user.save()

        with mock.patch.object(password_audit, "check_password", return_value=True) as checked:
            labels = password_audit.weak_account_labels(password_audit.admin_accounts(), [SENTINEL_WEAK_PASSWORD])

        assert labels == []
        checked.assert_not_called()

    def test_usable_password_does_reach_the_hash_comparison(self):
        """Reachability control for the test above.

        ``assert_not_called`` passes when the patch target is wrong, the query
        returns nothing, or the loop never runs. This asserts the same patched
        function IS called for an ordinary account, so a zero call count means
        the guard fired rather than the setup collapsing.
        """
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)

        with mock.patch.object(password_audit, "check_password", return_value=False) as checked:
            password_audit.weak_account_labels(password_audit.admin_accounts(), [SENTINEL_WEAK_PASSWORD])

        assert checked.call_count == 1

    def test_a_generator_of_candidates_is_not_drained_by_the_first_account(self):
        """``candidates`` is materialised before the loop; this is what binds it.

        Without the conversion the first account consumes the generator and
        every later one is tested against nothing -- reading clean. One
        account cannot show that, so this test needs two.
        """
        first = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        second = User.objects.create_user("editor", password=SENTINEL_WEAK_PASSWORD, is_staff=True)

        labels = password_audit.weak_account_labels(
            password_audit.admin_accounts().order_by("pk"),
            (candidate for candidate in ["alpha", SENTINEL_WEAK_PASSWORD]),
        )

        assert labels == [f"{first.pk}:root", f"{second.pk}:editor"]

    def test_first_usable_hash_skips_an_unusable_one(self):
        """Calibrating on an unusable hash measures Django's fake-runtime path."""
        locked = User.objects.create_superuser("locked", "locked@example.com", SENTINEL_WEAK_PASSWORD)
        locked.set_unusable_password()
        locked.save()
        usable = User.objects.create_user("editor", password=SENTINEL_WEAK_PASSWORD, is_staff=True)

        chosen = password_audit.first_usable_hash(password_audit.admin_accounts().order_by("pk"))

        assert chosen == User.objects.get(pk=usable.pk).password

    def test_first_usable_hash_is_none_when_every_account_is_locked(self):
        locked = User.objects.create_superuser("locked", "locked@example.com", SENTINEL_WEAK_PASSWORD)
        locked.set_unusable_password()
        locked.save()
        assert password_audit.first_usable_hash(password_audit.admin_accounts()) is None

    def test_label_carries_the_identifier_and_no_secret(self):
        user = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        label = password_audit.account_label(user)
        assert label == f"{user.pk}:root"
        assert not mentions(label, SENTINEL_WEAK_PASSWORD)


@override_settings(PASSWORD_HASHERS=[MD5_HASHER, PBKDF2_HASHER])
class CommandTests(TestCase):
    """``manage.py audit_admin_passwords``, driven by name.

    MD5 preferred here on purpose. Everything this class asserts is output
    plumbing, exit status and coverage reporting; ``check_password`` behaves
    identically whichever hasher produced the stored hash, so paying 190 ms a
    call buys nothing. ``WeakAccountLabelTests`` keeps the project's real
    hasher, so the audit is still exercised against a production-shaped hash.
    """

    def setUp(self):
        self.tmp_path = Path(self.enterContext(tmpdir()))
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        # Channels a future leak would plausibly use, which the injected
        # streams above cannot see. `capsys`/`capfd` are unavailable inside a
        # unittest.TestCase, so capture them directly.
        self.process_stdout = io.StringIO()
        self.process_stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.process_stdout))
        self.enterContext(contextlib.redirect_stderr(self.process_stderr))
        self.log_records = _RecordingHandler()
        self.enterContext(_root_logger_capture(self.log_records))

    def run_command(self, *candidates: str, **kwargs):
        path = write_list(self.tmp_path, *candidates)
        call_command(
            "audit_admin_passwords",
            password_file=str(path),
            stdout=self.stdout,
            stderr=self.stderr,
            **kwargs,
        )

    def captured(self, error: BaseException | None = None) -> str:
        """Everything the operator could see, from every channel we can reach.

        ``call_command(stdout=..., stderr=...)`` redirects only
        ``self.stdout``/``self.stderr``. A leak introduced later is far more
        likely to arrive as a leftover ``print()`` or a ``logger.info`` for
        progress -- neither of which touches those -- so the process streams
        and the root logger are captured too. Narrowing this to the injected
        streams let a ``print(f"matched: {candidate}")`` mutant survive the
        whole suite.
        """
        return "\n".join(
            [
                self.stdout.getvalue(),
                self.stderr.getvalue(),
                self.process_stdout.getvalue(),
                self.process_stderr.getvalue(),
                "\n".join(record.getMessage() for record in self.log_records.records),
                str(error or ""),
            ]
        )

    def test_is_discoverable_by_name(self):
        """Proves the command is reachable through INSTALLED_APPS discovery.

        ``load_command_class`` is the lookup ``manage.py`` itself performs. The
        other tests here go through ``call_command``, which would also accept a
        hand-built instance -- this one cannot.
        """
        command = load_command_class("abi_django_utils", "audit_admin_passwords")
        assert command.help

    def test_weak_account_fails_the_run(self):
        user = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        with pytest.raises(CommandError) as raised:
            self.run_command(SENTINEL_WEAK_PASSWORD)
        assert f"{user.pk}:root" in str(raised.value)
        # The message states its own redaction policy. Nothing behavioural
        # depends on that sentence, so without this it can be deleted green.
        assert "never passwords" in str(raised.value)
        assert "Reset them now" in str(raised.value)

    def test_output_never_contains_the_matched_password(self):
        """The assertion this ticket exists for -- with its two controls."""
        user = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        with pytest.raises(CommandError) as raised:
            self.run_command(SENTINEL_WEAK_PASSWORD)
        captured = self.captured(raised.value)

        # Reachability control: the failing account really was reported, so
        # the absence assertion below is about redaction and not about silence.
        assert f"{user.pk}:root" in captured
        # Checker control: `mentions` would have found the password if present.
        assert mentions(captured + SENTINEL_WEAK_PASSWORD, SENTINEL_WEAK_PASSWORD)

        assert not mentions(captured, SENTINEL_WEAK_PASSWORD)

    def test_the_leak_capture_can_see_every_channel_it_claims(self):
        """Control for the test above: each captured channel really is live.

        ``captured()`` concatenates six sources. If any one of them were wired
        to the wrong object, a leak down that channel would read as redacted
        and the test above would pass while disclosing a password. So write a
        marker down each channel and assert it comes back.
        """
        print(SENTINEL_WEAK_PASSWORD, end="")
        print(SENTINEL_WEAK_PASSWORD, end="", file=sys.stderr)
        logging.getLogger("abi_django_utils.test").warning(SENTINEL_WEAK_PASSWORD)
        self.stdout.write(SENTINEL_WEAK_PASSWORD)
        self.stderr.write(SENTINEL_WEAK_PASSWORD)

        assert self.process_stdout.getvalue() == SENTINEL_WEAK_PASSWORD
        assert self.process_stderr.getvalue() == SENTINEL_WEAK_PASSWORD
        assert [record.getMessage() for record in self.log_records.records] == [SENTINEL_WEAK_PASSWORD]
        assert mentions(self.captured(), SENTINEL_WEAK_PASSWORD)

    def test_reports_every_weak_account_not_just_the_first(self):
        """Each app plausibly has more than one staff account.

        With a single weak account, ``weak[:1]`` and ``", ".join(weak)``
        versus ``weak[0]`` are all indistinguishable.
        """
        first = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        second = User.objects.create_user("editor", password=SENTINEL_WEAK_PASSWORD, is_staff=True)
        strong = User.objects.create_user("safe", password="tessellate-marmoset-quorum-8814", is_staff=True)

        with pytest.raises(CommandError) as raised:
            self.run_command(SENTINEL_WEAK_PASSWORD)
        message = str(raised.value)

        assert f"{first.pk}:root" in message
        assert f"{second.pk}:editor" in message
        assert f"{strong.pk}:safe" not in message
        assert "2 admin account(s)" in message

    def test_exits_non_zero_through_the_real_entry_point(self):
        """``call_command`` raises; ``manage.py`` exits. The ticket says exits.

        ``run_from_argv`` is the path a cron job or CI step actually takes,
        and it is the only one that both converts ``CommandError`` into an
        exit status and runs argparse's ``type=`` coercions. A
        ``CommandError(..., returncode=0)`` survives every ``call_command``
        test in this file.
        """
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        path = write_list(self.tmp_path, SENTINEL_WEAK_PASSWORD)
        command = load_command_class("abi_django_utils", "audit_admin_passwords")
        # Wrapped as ``BaseCommand.__init__`` would: ``run_from_argv`` runs the
        # system checks, and any warning they print (axes's, for one) calls
        # ``stderr.write(msg, style_func)``, which a bare StringIO rejects.
        command.stdout, command.stderr = OutputWrapper(self.stdout), OutputWrapper(self.stderr)

        with pytest.raises(SystemExit) as raised:
            command.run_from_argv(["manage.py", "audit_admin_passwords", "--password-file", str(path), "--limit", "1"])

        assert raised.value.code != 0

    def test_empty_candidate_list_is_an_error_not_a_clean_audit(self):
        """A green audit that tested nothing is worse than a red one."""
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        with pytest.raises(CommandError) as raised:
            self.run_command()
        assert "refusing to report a clean audit" in str(raised.value)

    def test_whitespace_only_candidate_list_is_also_an_error(self):
        """The blank-line filter must not turn a junk file into an empty pass."""
        path = self.tmp_path / "candidates.txt"
        path.write_text("\n   \n\n", encoding="utf-8")
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        with pytest.raises(CommandError):
            call_command(
                "audit_admin_passwords",
                password_file=str(path),
                stdout=self.stdout,
                stderr=self.stderr,
            )

    def test_no_admin_accounts_warns_rather_than_passing_quietly(self):
        """Legitimate for an app with no staff; also what a wrong DB looks like."""
        self.run_command("alpha")
        assert "nothing was audited" in self.stdout.getvalue()
        assert "DJANGO_SETTINGS_MODULE" in self.stdout.getvalue()

    def test_a_truncating_limit_says_what_it_did_not_test(self):
        """A clean run under --limit is a claim about the top N, not the list."""
        User.objects.create_superuser("root", "root@example.com", "tessellate-marmoset-quorum-8814")
        self.run_command("alpha", "beta", "gamma", limit=2)
        assert "--limit stopped at 2" in self.stdout.getvalue()
        assert "Pass --limit 0" in self.stdout.getvalue()

    def test_an_untruncated_limit_does_not_claim_a_gap(self):
        """Reachability control for the test above: the notice is conditional."""
        User.objects.create_superuser("root", "root@example.com", "tessellate-marmoset-quorum-8814")
        self.run_command("alpha", "beta", limit=50)
        assert "--limit stopped at" not in self.stdout.getvalue()

    def test_each_finding_is_written_as_it_is_found(self):
        """An hour-long run must not hold its results until the end."""
        user = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        with pytest.raises(CommandError):
            self.run_command(SENTINEL_WEAK_PASSWORD)
        assert f"WEAK: {user.pk}:root" in self.stderr.getvalue()

    def test_strong_password_exits_clean(self):
        User.objects.create_superuser("root", "root@example.com", "correct-horse-battery-staple-7731")
        self.run_command(SENTINEL_WEAK_PASSWORD)
        assert "No admin account matched" in self.stdout.getvalue()

    def test_ordinary_user_with_a_weak_password_is_not_audited(self):
        User.objects.create_user("reader", password=SENTINEL_WEAK_PASSWORD)
        self.run_command(SENTINEL_WEAK_PASSWORD)
        assert "No admin account matched" in self.stdout.getvalue()

    def test_caveat_is_printed_on_a_clean_run(self):
        """A clean run is the result most likely to be over-read."""
        User.objects.create_superuser("root", "root@example.com", "correct-horse-battery-staple-7731")
        self.run_command(SENTINEL_WEAK_PASSWORD)
        assert password_audit.COVERAGE_CAVEAT in self.stdout.getvalue()

    def test_caveat_is_printed_on_a_failing_run(self):
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        with pytest.raises(CommandError):
            self.run_command(SENTINEL_WEAK_PASSWORD)
        assert password_audit.COVERAGE_CAVEAT in self.stdout.getvalue()

    def test_caveat_names_every_limit_it_claims(self):
        """The caveat is the command's whole honesty budget, so pin each clause.

        Three separate claims, three separate assertions. Pinning only two of
        them let a mutant delete the third -- "a password absent from the list
        tested here may still be guessable" -- with the suite still green.
        """
        for claim in (
            "can prove an account's password weak",
            "may still be guessable",
            "cannot prove one strong",
            "lowercased",
        ):
            assert claim in password_audit.COVERAGE_CAVEAT, claim

    def test_reports_how_many_candidates_were_tested(self):
        """A clean run's meaning depends on the coverage, so it is in the output."""
        User.objects.create_superuser("root", "root@example.com", "correct-horse-battery-staple-7731")
        self.run_command("alpha", "beta", "gamma")
        assert "Candidates to test: 3" in self.stdout.getvalue()
        assert "No admin account matched the 3 candidates tested." in self.stdout.getvalue()

    def test_reports_a_runtime_estimate_before_working(self):
        User.objects.create_superuser("root", "root@example.com", "correct-horse-battery-staple-7731")
        self.run_command("alpha")
        output = self.stdout.getvalue()
        assert "ms per check" in output
        assert output.index("ms per check") < output.index("No admin account matched")

    def test_no_admin_accounts_is_a_clean_run_without_an_estimate(self):
        """No accounts means no hash to calibrate against; must not divide by zero."""
        self.run_command("alpha")
        assert "Admin accounts (is_staff or is_superuser): 0" in self.stdout.getvalue()
        assert "ms per check" not in self.stdout.getvalue()

    def test_limit_zero_means_the_whole_file(self):
        User.objects.create_superuser("root", "root@example.com", "correct-horse-battery-staple-7731")
        self.run_command("alpha", "beta", limit=0)
        assert "Candidates to test: 2" in self.stdout.getvalue()

    def test_limit_truncates_the_candidate_list(self):
        User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        self.run_command("alpha", SENTINEL_WEAK_PASSWORD, limit=1)
        assert "Candidates to test: 1" in self.stdout.getvalue()
        assert "No admin account matched" in self.stdout.getvalue()


class SharedValidatorTests(SimpleTestCase):
    """The preventive half, exercised through ``validate_password``.

    ``django_defaults()`` already carries the four validators. Nothing before
    this class proved any of them reachable: the existing test asserts the list
    has four dicts with a ``NAME`` key, which passes if every entry names a
    validator that does not exist.
    """

    def assert_rejected(self, password: str, code: str):
        """Assert the shared validator set refuses ``password``, for ``code``.

        The code matters as much as the rejection: three of the four
        validators would reject a short numeric common password, and a test
        that only asserted "raised" could not tell which one was live.
        """
        with override_settings(AUTH_PASSWORD_VALIDATORS=SHARED_VALIDATORS):
            with pytest.raises(ValidationError) as raised:
                validate_password(password)
        codes = [error.code for error in raised.value.error_list]
        assert code in codes, codes

    def test_rejects_a_common_password(self):
        self.assert_rejected("football1", "password_too_common")

    def test_rejects_a_capitalised_common_password(self):
        """The asymmetry the audit cannot reproduce: the validator lowercases."""
        self.assert_rejected("Football1", "password_too_common")

    def test_rejects_a_short_password(self):
        self.assert_rejected("Gx7!q", "password_too_short")

    def test_rejects_an_entirely_numeric_password(self):
        self.assert_rejected("48201947205", "password_entirely_numeric")

    def test_accepts_a_strong_password(self):
        """Reachability control: proves the set above is not refusing everything."""
        with override_settings(AUTH_PASSWORD_VALIDATORS=SHARED_VALIDATORS):
            validate_password("tessellate-marmoset-quorum-8814")

    def test_shared_defaults_include_the_common_password_validator(self):
        names = [entry["NAME"] for entry in SHARED_VALIDATORS]
        assert "django.contrib.auth.password_validation.CommonPasswordValidator" in names
