"""Dictionary audit of admin-account passwords.

A stored password is a salted hash, so the only way to ask "is this account's
password guessable" is to guess: hash each candidate with that account's salt
and compare. That shape has a consequence worth stating in the module that
implements it -- **this code can prove an account weak and can never prove one
strong.** A clean result means "not in the list we tested", nothing more.

Two of those limits are sharp enough to name:

* **Case.** The list Django ships is lowercased and deduplicated. Its
  ``CommonPasswordValidator`` lowercases the submitted password before looking
  it up, so it refuses ``Password1``; a hash comparison cannot do that, so an
  account whose password is ``Password1`` reads clean here. The preventive
  half catches a strictly wider set than the audit half.
* **Coverage.** See :data:`DEFAULT_CANDIDATE_LIMIT`.

Two design choices follow from the requirement that the audit must never
disclose a password (issue #17):

* :func:`password_is_common` takes an *encoded hash* and returns a ``bool``.
  It never receives a user and never returns the candidate that matched, so
  there is no plaintext in scope for a caller to render by accident. The
  secrecy property is a property of the signature rather than of the caller's
  discipline.
* :func:`weak_account_labels` yields identifiers only.

Cost is the other thing a caller has to know, and it is version-dependent.
Measured on Django 6.0.4 (PBKDF2-SHA256, 1.2M iterations) one
:func:`check_password` call costs ~190 ms on an M-series Mac, so its
19,640-entry list is ~63 minutes *per account*. Django 4.2 ships 20,000
entries at 600k iterations, so roughly half that. Nothing here assumes either
figure: :func:`seconds_per_check` measures the real cost at runtime against
the account's own hash, and the command prints the estimate before it goes
quiet. :data:`DEFAULT_CANDIDATE_LIMIT` stops at the most common 1,000 and the
command says so, because a clean run's meaning depends entirely on how far it
looked.
"""

from __future__ import annotations

import gzip
import time
from collections.abc import Iterable, Iterator
from itertools import islice
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, is_password_usable
from django.contrib.auth.password_validation import CommonPasswordValidator
from django.db.models import Q

#: How many candidates to try when the caller does not say. The list is
#: ordered most-common-first, so a limit is a coverage choice and not an
#: arbitrary slice -- see :func:`read_password_list`.
DEFAULT_CANDIDATE_LIMIT = 1000

#: Printed by the management command on every run, pass or fail. A clean run is
#: the result most likely to be misread, so the caveat is not conditional.
COVERAGE_CAVEAT = (
    "A dictionary test can prove an account's password weak. It cannot prove one strong: "
    "a password absent from the list tested here may still be guessable, and because the "
    "list is lowercased, a capitalised variant of a common password reads clean here even "
    "though CommonPasswordValidator would refuse it."
)

#: Timing probe. Not a real candidate -- it exists only to cost one hash.
_CALIBRATION_CANDIDATE = "abi-django-utils-calibration-probe"


def default_password_list_path() -> Path:
    """Return the path of the list Django ships for ``CommonPasswordValidator``.

    Read off the validator rather than rebuilt from ``django.contrib.auth``'s
    ``__file__``, so the audit and the preventive validator are provably
    pointed at one file.

    Accessed through an *instance*, and that is load-bearing. The attribute is
    a ``cached_property`` on every Django this library supports -- verified on
    4.2.30, 5.0.14, 5.2.18 and 6.0.4 -- so class access yields the descriptor
    object rather than a path and ``Path()`` raises ``TypeError``. There is no
    version here on which the shorter spelling works.

    It costs one read of the 80 KB list, which also proves the file is
    readable before we promise a caller an audit.
    """
    return Path(CommonPasswordValidator().DEFAULT_PASSWORD_LIST_PATH)


def read_password_list(path: Path, *, limit: int | None = DEFAULT_CANDIDATE_LIMIT) -> Iterator[str]:
    """Yield up to ``limit`` non-empty lines from a password list.

    Handles gzipped and plain files. Order is preserved, which is the whole
    point of a limit: Django's list runs in descending frequency order
    (``123456`` first), so the first N are the N most common rather than an
    arbitrary slice. ``limit=None`` yields the whole file.

    Compression is detected by trying to decompress, the way
    ``CommonPasswordValidator`` does it, rather than by looking at the
    filename. A ``.gz`` suffix is a convention and ``--password-file
    list.txt.gzip`` is not unusual; opened as text with errors suppressed, a
    gzip stream decodes into junk and the audit reports a confident clean run
    against a list of garbage. Decode errors are likewise left to raise: "this
    file is not a password list" must not round down to "here are some
    candidates".
    """
    path = Path(path)  # a caller (or call_command kwargs) may hand us a str
    with _open_text(path) as handle:
        stripped = (line.strip() for line in handle)
        non_empty = (candidate for candidate in stripped if candidate)
        yield from islice(non_empty, limit)


def _open_text(path: Path):
    """Open ``path`` as UTF-8 text, decompressing it if it is gzipped.

    ``OSError`` rather than ``BadGzipFile`` so this mirrors
    ``CommonPasswordValidator.__init__``: a missing path fails the fallback
    too and raises its own ``FileNotFoundError`` from there.
    """
    handle = gzip.open(path, "rt", encoding="utf-8")
    try:
        handle.read(1)
    except OSError:
        handle.close()
        return open(path, encoding="utf-8")
    handle.seek(0)
    return handle


def load_candidates(path: Path | None = None, *, limit: int | None = DEFAULT_CANDIDATE_LIMIT) -> list[str]:
    """Return up to ``limit`` candidates from ``path``, or from Django's list."""
    return list(read_password_list(path or default_password_list_path(), limit=limit))


def admin_accounts(user_model=None):
    """Return the accounts that can reach ``/admin/`` -- staff or superuser."""
    model = user_model or get_user_model()
    return model._default_manager.filter(Q(is_staff=True) | Q(is_superuser=True))


def password_is_common(encoded: str, candidates: Iterable[str]) -> bool:
    """Is ``encoded`` the hash of any of ``candidates``?

    Takes a hash, returns a bool. The matching candidate is deliberately not
    returned -- see the module docstring.
    """
    return any(check_password(candidate, encoded) for candidate in candidates)


def iter_weak_account_labels(users: Iterable, candidates: Iterable[str]) -> Iterator[str]:
    """Yield identifiers of accounts whose password is in ``candidates``.

    Identifiers only, never the matched password. Accounts with an unusable
    password (``!``-prefixed, i.e. login disabled) cannot match anything, so
    they are skipped rather than charged a thousand hashes to prove it.

    A generator because a full-list audit runs for tens of minutes per
    account: the command writes each finding as it arrives, so an operator who
    loses the session still has the findings that had been reached.

    ``candidates`` is materialised first -- a generator argument would be
    drained by the first account and every later one would be tested against
    nothing and read clean.
    """
    candidates = list(candidates)
    for user in users:
        if is_password_usable(user.password) and password_is_common(user.password, candidates):
            yield account_label(user)


def weak_account_labels(users: Iterable, candidates: Iterable[str]) -> list[str]:
    """Collect :func:`iter_weak_account_labels` for callers that want a list."""
    return list(iter_weak_account_labels(users, candidates))


def first_usable_hash(users: Iterable) -> str | None:
    """Return a hash worth calibrating against, or ``None`` if there is none.

    Calibrating on an unusable or unidentifiable hash measures Django's
    fake-runtime path -- the *default* hasher's cost rather than that
    account's -- which skews an estimate the operator is being asked to trust.
    """
    for user in users:
        if is_password_usable(user.password):
            return user.password
    return None


def seconds_per_check(encoded: str) -> float:
    """Measure one hash verification against ``encoded``.

    Turns a candidate count into an estimate the operator sees *before* the
    command goes quiet for minutes. Hash cost is a property of the stored
    hash's own parameters, so it has to be measured against a real one rather
    than assumed from the project's current ``PASSWORD_HASHERS``.
    """
    started = time.perf_counter()
    check_password(_CALIBRATION_CANDIDATE, encoded)
    return time.perf_counter() - started


def estimate_seconds(*, accounts: int, candidates: int, seconds_per_check: float) -> float:
    """Worst-case runtime: every account tested against every candidate."""
    return accounts * candidates * seconds_per_check


def account_label(user) -> str:
    """Identify an account in output without disclosing anything secret."""
    return f"{user.pk}:{user.get_username()}"
