"""``manage.py audit_admin_passwords`` -- dictionary-test every admin account.

Requires ``"abi_django_utils"`` in ``INSTALLED_APPS``; Django discovers
management commands by walking installed apps, so an app that imports this
library's helpers directly but does not install it will not find this command.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from abi_django_utils import password_audit


class Command(BaseCommand):
    help = "Report admin accounts whose password appears in a common-password list."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password-file",
            type=Path,
            default=None,
            help="Newline-delimited list, gzipped or plain. Defaults to Django's own ~20,000-entry list.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=password_audit.DEFAULT_CANDIDATE_LIMIT,
            help=(
                "Test only the first N candidates. The list is ordered most-common-first, so this is a "
                f"coverage dial. 0 means the whole file. Default {password_audit.DEFAULT_CANDIDATE_LIMIT}."
            ),
        )

    def handle(self, *args, **options):
        limit = options["limit"] or None
        candidates = password_audit.load_candidates(options["password_file"], limit=limit)
        accounts = list(password_audit.admin_accounts())

        self._refuse_an_empty_audit(accounts, candidates, options["password_file"])
        self._report_plan(accounts, candidates, limit)
        weak = self._audit(accounts, candidates)
        self.stdout.write(password_audit.COVERAGE_CAVEAT)

        if weak:
            raise CommandError(self._failure_message(weak, len(candidates)))
        self.stdout.write(self.style.SUCCESS(f"No admin account matched the {len(candidates)} candidates tested."))

    def _audit(self, accounts, candidates) -> list[str]:
        """Run the audit, writing each finding as it arrives.

        A full-list audit is tens of minutes per account, so a result that
        only exists at the end is a result a dropped session throws away.
        Labels carry no secret, so streaming them costs nothing.
        """
        weak = []
        for label in password_audit.iter_weak_account_labels(accounts, candidates):
            self.stderr.write(f"WEAK: {label}")
            weak.append(label)
        return weak

    def _refuse_an_empty_audit(self, accounts, candidates, path) -> None:
        """Fail rather than report a clean audit of nothing.

        The exit code is this command's whole product -- it is built to run
        from CI or cron. A typo'd ``--password-file``, a truncated download or
        an empty mounted volume otherwise yields exit 0 and "No admin account
        matched the 0 candidates tested", which is a green security audit that
        tested nothing. Zero admin accounts is a warning rather than an error:
        an app with no staff is legitimate, but it is also what a wrong
        ``DJANGO_SETTINGS_MODULE`` or ``DATABASE_URL`` looks like.
        """
        if not candidates:
            source = path or "Django's own list"
            raise CommandError(f"No candidates loaded from {source}; refusing to report a clean audit.")
        if not accounts:
            self.stdout.write(
                self.style.WARNING(
                    "No account has is_staff or is_superuser set, so nothing was audited. "
                    "If you expected admin accounts here, check DJANGO_SETTINGS_MODULE and the database."
                )
            )

    def _report_plan(self, accounts, candidates, limit) -> None:
        """Say what is about to happen, with a measured runtime estimate.

        Written before the work rather than after, because at Django's default
        hasher cost the work can take an hour and the operator otherwise has
        no way to tell a long run from a hung one.
        """
        self.stdout.write(f"Admin accounts (is_staff or is_superuser): {len(accounts)}")
        self.stdout.write(f"Candidates to test: {len(candidates)}")
        if limit is not None and len(candidates) == limit:
            self.stdout.write(
                f"--limit stopped at {limit}, so anything past the {limit} most common was not tested. "
                "Pass --limit 0 to audit the whole list."
            )
        calibration_hash = password_audit.first_usable_hash(accounts)
        if calibration_hash is None:
            return
        per_check = password_audit.seconds_per_check(calibration_hash)
        estimate = password_audit.estimate_seconds(
            accounts=len(accounts),
            candidates=len(candidates),
            seconds_per_check=per_check,
        )
        self.stdout.write(f"Measured {per_check * 1000:.0f} ms per check; worst case ~{estimate / 60:.1f} min.")

    def _failure_message(self, weak: list[str], tested: int) -> str:
        """Name the failing accounts and nothing else.

        ``weak`` holds ``pk:username`` labels from
        :func:`~abi_django_utils.password_audit.account_label`; no password
        material reaches this method, which is why it cannot leak any.
        """
        listed = ", ".join(weak)
        return (
            f"{len(weak)} admin account(s) have a password in the {tested} tested: {listed}. "
            "Reset them now. This message names accounts only, never passwords."
        )
