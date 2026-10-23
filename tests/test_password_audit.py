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

import gzip
import io
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from abi_django_utils import defaults, password_audit

#: Distinctive enough that finding it in command output cannot be a
#: coincidence, and short enough to cost one hash per check.
SENTINEL_WEAK_PASSWORD = "zz-sentinel-weak-pw-9471"

SHARED_VALIDATORS = defaults.django_defaults()["AUTH_PASSWORD_VALIDATORS"]


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

    def test_seconds_per_check_measures_rather_than_assumes(self):
        """A constant would satisfy every caller and be wrong on any other box.

        Compared against a hash verification timed here, so the test asserts
        the function reports what it did rather than a plausible number. Loose
        bounds (4x either way) because both samples are single timings on a
        shared machine.
        """
        encoded = make_password("x")
        started = time.perf_counter()
        check_password("y", encoded)
        directly_timed = time.perf_counter() - started

        measured = password_audit.seconds_per_check(encoded)

        assert measured > 0
        assert directly_timed / 4 < measured < directly_timed * 4


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

    def test_label_carries_the_identifier_and_no_secret(self):
        user = User.objects.create_superuser("root", "root@example.com", SENTINEL_WEAK_PASSWORD)
        label = password_audit.account_label(user)
        assert label == f"{user.pk}:root"
        assert not mentions(label, SENTINEL_WEAK_PASSWORD)


class CommandTests(TestCase):
    """``manage.py audit_admin_passwords``, driven by name."""

    def setUp(self):
        self.tmp_path = Path(self.enterContext(tmpdir()))
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

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
        """Everything the operator could see, including the raised message."""
        return "\n".join([self.stdout.getvalue(), self.stderr.getvalue(), str(error or "")])

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

    def test_caveat_names_both_limits_it_claims(self):
        """The caveat is the command's whole honesty budget; pin its content."""
        assert "cannot prove one strong" in password_audit.COVERAGE_CAVEAT
        assert "lowercased" in password_audit.COVERAGE_CAVEAT

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
