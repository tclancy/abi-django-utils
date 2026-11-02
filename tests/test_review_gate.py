"""Unit tests for the review gate shipped by issue #24.

The gate is three files that have to agree:

* ``.github/review-gate.jq`` -- the decision, as a pure function.
* ``.github/workflows/require-review.yml`` -- fetches the inputs and publishes
  the verdict as a commit status on the PR head.
* ``.github/workflows/claude-code-review.yml`` -- produces the review the gate
  looks for.

These are ported from ``tclancy/bluelinky``'s ``__tests__/review-gate.spec.ts``
(PR #52), which is the precedent for this rollout. That repo is TypeScript and
this one is Python, so the assertions are reimplemented rather than copied.

Every test that grades a verdict drives the **committed** jq program with
``jq -f``. None of them reimplement the rule in Python -- a test holding its
own copy of the logic passes while the shipped file is broken.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / ".github" / "review-gate.jq"
REVIEWER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-code-review.yml"
STATUS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "require-review.yml"

HEAD_AT = "2026-10-25T03:00:00Z"
BEFORE_HEAD = "2026-10-25T02:00:00Z"
AFTER_HEAD = "2026-10-25T04:00:00Z"

JQ = shutil.which("jq")

# A skipped gate test reads exactly like a passing one. That trade is fine on a
# laptop and is not fine in CI, which is the only run anyone grades a PR on --
# so in CI a missing ``jq`` is a failure, not a skip.
#
# THIS IS AN IMPORT-TIME RAISE AND NOT A TEST, because a test cannot do this
# job. An earlier draft asserted it in ``test_jq_is_available_...`` and guarded
# the module with ``pytestmark = pytest.mark.skipif(JQ is None, ...)``. A
# module-level ``pytestmark`` applies to EVERY collected item regardless of
# definition order, so it covered the control too: measured 2026-10-25, with
# ``CI=true`` and ``jq`` absent that module reported **53 skipped, exit 0** --
# the one test whose entire job was to prevent a silent skip, silently skipped.
#
# ``ubuntu-latest`` ships jq, so this asserts a property of the runner rather
# than adding a dependency.
if os.environ.get("CI") and JQ is None:
    raise RuntimeError(
        "jq is missing from this runner, so every verdict test in this module would "
        "silently skip and the review gate would ship ungraded. ubuntu-latest ships "
        "jq; if this fires, the runner image changed."
    )


def grade(**overrides) -> dict:
    """Run the committed jq program over one gate input and return the verdict.

    The skip lives here rather than on a module-level mark so that it reaches
    exactly the tests that need jq -- see the import-time raise above for what
    the mark did to the control that was supposed to backstop it. The ~20
    workflow-property tests below parse YAML and need no jq at all.
    """
    if JQ is None:
        pytest.skip("jq is not installed")
    payload = {
        "number": 42,
        "head_committed_at": HEAD_AT,
        "author": "metaframework-dispatch-bot",
        "title": "feat: something",
        "bot_logins": ["claude[bot]"],
        "comments": [],
        "reviews": [],
    }
    payload.update(overrides)
    result = subprocess.run(
        [JQ, "-f", str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def comment(body: str, login: str = "claude[bot]", updated_at: str | None = AFTER_HEAD) -> dict:
    return {"user": {"login": login}, "body": body, "updated_at": updated_at}


def review(state: str, login: str = "tclancy", submitted_at: str | None = AFTER_HEAD) -> dict:
    return {"user": {"login": login}, "state": state, "submitted_at": submitted_at}


# --------------------------------------------------------------------------
# No review exists
# --------------------------------------------------------------------------


def test_fails_a_pr_with_no_comments_and_no_reviews():
    assert grade()["state"] == "failure"


def test_ignores_a_comment_from_a_non_bot_login():
    assert grade(comments=[comment("Looks good to me", login="some-human")])["state"] == "failure"


def test_rejects_a_login_that_is_merely_a_prefix_of_the_allow_listed_bot():
    """``["claude[bot]"] | index("claude")`` must not match.

    jq's ``index`` does a subsequence search when handed an array and an exact
    element match when handed a scalar, so the obvious spelling of this
    predicate admits every login. See the note on ``is_bot`` in the gate.
    """
    assert grade(comments=[comment("Reviewed, all good", login="claude")])["state"] == "failure"


def test_rejects_a_login_that_merely_contains_the_allow_listed_bot():
    assert grade(comments=[comment("Reviewed, all good", login="not-claude[bot]-really")])["state"] == "failure"


def test_does_not_count_a_commented_review_as_a_review():
    assert grade(reviews=[review("COMMENTED")])["state"] == "failure"


def test_does_not_count_a_dismissed_review_as_a_review():
    assert grade(reviews=[review("DISMISSED")])["state"] == "failure"


# --------------------------------------------------------------------------
# A review exists for this head
# --------------------------------------------------------------------------


def test_passes_on_a_bot_comment_newer_than_the_head_commit():
    verdict = grade(comments=[comment("Reviewed. Two nits inline.")])
    assert verdict["state"] == "success"
    assert "claude[bot]" in verdict["description"]


def test_passes_on_a_formal_approved_review():
    assert grade(reviews=[review("APPROVED")])["state"] == "success"


def test_passes_on_a_formal_changes_requested_review():
    assert grade(reviews=[review("CHANGES_REQUESTED")])["state"] == "success"


def test_counts_a_review_submitted_in_the_same_second_as_the_head_commit():
    """``>=``, not ``>``. A review landing in the head commit's own second is
    not stale, and second-granularity timestamps make ties ordinary."""
    assert grade(reviews=[review("APPROVED", submitted_at=HEAD_AT)])["state"] == "success"


def test_counts_a_review_whose_timestamp_is_null():
    assert grade(reviews=[review("APPROVED", submitted_at=None)])["state"] == "success"


def test_counts_a_review_when_the_head_commit_date_is_unavailable():
    verdict = grade(
        head_committed_at=None,
        reviews=[review("APPROVED", submitted_at=BEFORE_HEAD)],
    )
    assert verdict["state"] == "success"


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------


def test_fails_when_every_review_predates_the_head_commit():
    verdict = grade(
        comments=[comment("Reviewed.", updated_at=BEFORE_HEAD)],
        reviews=[review("APPROVED", submitted_at=BEFORE_HEAD)],
    )
    assert verdict["state"] == "failure"


def test_distinguishes_a_stale_review_from_no_review_at_all():
    """The two refusals have different causes and different fixes, and the
    description is the highest-traffic instruction site in the gate."""
    stale = grade(reviews=[review("APPROVED", submitted_at=BEFORE_HEAD)])["description"]
    none = grade()["description"]
    assert stale != none
    assert "predate" in stale
    assert "No code review" in none


def test_offers_review_as_the_remedy_in_both_refusals():
    stale = grade(reviews=[review("APPROVED", submitted_at=BEFORE_HEAD)])["description"]
    none = grade()["description"]
    assert "/review" in stale
    assert "/review" in none


def test_interpolates_the_real_pr_number_into_the_no_review_remedy():
    assert "#1337" in grade(number=1337)["description"]


def test_passes_when_a_stale_review_is_accompanied_by_a_fresh_one():
    verdict = grade(
        comments=[
            comment("Old pass", updated_at=BEFORE_HEAD),
            comment("Reviewed this head", updated_at=AFTER_HEAD),
        ]
    )
    assert verdict["state"] == "success"
    assert AFTER_HEAD in verdict["description"]


# --------------------------------------------------------------------------
# A run that started is not a review
# --------------------------------------------------------------------------

IN_FLIGHT = "Claude Code is working… <img src='https://example.invalid/spinner.gif' />"
ERRORED_NO_CHECKLIST = "**Claude encountered an error** —— [View job](https://example.invalid/run/1)"
ERRORED_PARTIAL = "**Claude encountered an error after 2m 10s**\n\n- [x] Read the diff\n- [ ] Post findings"
ERRORED_COMPLETE = "**Claude encountered an error after 9m 02s**\n\n- [x] Read the diff\n- [x] Post findings"
FINISHED = "**Claude finished reviewing in 3m 48s**\n\n- [x] Read the diff\n- [x] Post findings"
FINISHED_NO_CHECKLIST = "**Claude finished reviewing in 3m 48s** \u2014 [View job](https://example.invalid/run/2)"
PLAIN_SUMMARY = "Reviewed the diff. One finding posted inline; nothing else worth flagging."


def test_fails_on_the_actions_in_flight_comment_which_has_no_checklist_at_all():
    assert grade(comments=[comment(IN_FLIGHT)])["state"] == "failure"


def test_fails_on_an_errored_run_that_posted_no_checklist():
    assert grade(comments=[comment(ERRORED_NO_CHECKLIST)])["state"] == "failure"


def test_fails_on_an_errored_run_that_died_partway_through_its_checklist():
    assert grade(comments=[comment(ERRORED_PARTIAL)])["state"] == "failure"


def test_passes_an_errored_run_that_finished_its_checklist_anyway():
    """metaframework#560: an ``error_max_turns`` run has read the diff and
    posted its findings before blowing its budget."""
    assert grade(comments=[comment(ERRORED_COMPLETE)])["state"] == "success"


def test_passes_the_finished_tracking_comment():
    assert grade(comments=[comment(FINISHED)])["state"] == "success"


def test_passes_a_finished_status_comment_that_carries_no_checklist():
    """sandy#175's shape, and the fixture a mutation round demanded.

    ``counts_as_review`` admits an action status comment on EITHER "finished"
    or a completed checklist. ``FINISHED`` satisfies both, so with it as the
    only fixture the ``says_finished`` half is never contested -- measured
    2026-10-25, a mutant replacing ``says_finished`` with ``false`` SURVIVED a
    round that killed 21 of its 22 siblings. This fixture contests it.
    """
    assert "- [" not in FINISHED_NO_CHECKLIST
    assert grade(comments=[comment(FINISHED_NO_CHECKLIST)])["state"] == "success"


def test_control_each_half_of_the_action_status_rule_is_independently_sufficient():
    """Neither fixture satisfies both clauses, so each one grades one clause."""
    assert not ERRORED_COMPLETE.startswith("**Claude finished")
    assert "- [x]" not in FINISHED_NO_CHECKLIST
    assert grade(comments=[comment(ERRORED_COMPLETE)])["state"] == "success"
    assert grade(comments=[comment(FINISHED_NO_CHECKLIST)])["state"] == "success"


def test_passes_a_plain_summary_comment_that_carries_no_checklist_at_all():
    assert grade(comments=[comment(PLAIN_SUMMARY)])["state"] == "success"


def test_control_two_checklistless_shapes_are_graded_oppositely():
    """Without this control the suite cannot tell the rule from "no checklist
    means fail". ``ERRORED_NO_CHECKLIST`` and ``PLAIN_SUMMARY`` both lack a
    checklist; only the first is one of the action's own status comments."""
    assert "- [" not in ERRORED_NO_CHECKLIST
    assert "- [" not in PLAIN_SUMMARY
    assert grade(comments=[comment(ERRORED_NO_CHECKLIST)])["state"] == "failure"
    assert grade(comments=[comment(PLAIN_SUMMARY)])["state"] == "success"


def test_counts_a_summary_that_quotes_an_action_status_line_mid_prose():
    body = "I checked the run: it said **Claude encountered an error** but the findings landed. LGTM."
    assert grade(comments=[comment(body)])["state"] == "success"


def test_counts_a_review_that_mentions_an_unticked_box_mid_sentence():
    body = "The PR body still has a `- [ ] ` item in its plan, which is fine. Reviewed, no findings."
    assert grade(comments=[comment(body)])["state"] == "success"


def test_rejects_a_body_that_is_nothing_but_an_outstanding_checklist_item():
    assert grade(comments=[comment("- [ ] Review the diff")])["state"] == "failure"


def test_does_not_count_an_empty_comment_body():
    assert grade(comments=[comment("")])["state"] == "failure"


def test_scores_a_bot_comment_on_when_it_was_last_edited_not_when_it_was_created():
    """``track_progress`` creates the checklist unticked and edits it to
    completion, so ``created_at`` is not when the review existed."""
    stale_creation = {
        "user": {"login": "claude[bot]"},
        "body": FINISHED,
        "created_at": BEFORE_HEAD,
        "updated_at": AFTER_HEAD,
    }
    assert grade(comments=[stale_creation])["state"] == "success"


# --------------------------------------------------------------------------
# The one exemption is on the author
# --------------------------------------------------------------------------


def test_exempts_a_dependabot_pr():
    verdict = grade(author="dependabot[bot]")
    assert verdict["state"] == "success"
    assert "Dependabot" in verdict["description"]


def test_does_not_exempt_a_chore_titled_pr():
    """A title prefix is a one-word self-service bypass for any agent."""
    assert grade(title="chore: bump the lockfile")["state"] == "failure"


def test_does_not_exempt_a_title_that_merely_contains_chore():
    assert grade(title="fix: the chore runner drops its last item")["state"] == "failure"


# --------------------------------------------------------------------------
# The published description
# --------------------------------------------------------------------------


def test_control_the_stale_refusal_really_does_overflow_140_characters():
    """Reachability control for the truncation tests below.

    If this fixture ever stops overflowing, the two assertions that follow
    become vacuous -- they would pass against a gate with no truncation at all.
    The tell that truncation happened on a real overflow rather than on nothing
    is that information present in the INPUT is missing from the output.
    """
    verdict = grade(reviews=[review("APPROVED", submitted_at=BEFORE_HEAD)])
    assert len(verdict["description"]) == 140
    assert verdict["description"].endswith("...")
    # Truncation cuts the TAIL, so the evidence of a real overflow is that the
    # refusal's closing clause is missing while its opening is present. The
    # remedy itself survives, which is why `/review` is still assertable above.
    assert "re-review this head." not in verdict["description"]
    assert verdict["description"].startswith("Reviews on this PR all predate")


def test_never_exceeds_140_characters():
    for verdict in (
        grade(),
        grade(author="dependabot[bot]"),
        grade(comments=[comment(FINISHED)]),
        grade(reviews=[review("APPROVED", submitted_at=BEFORE_HEAD)]),
    ):
        assert len(verdict["description"]) <= 140


def test_leaves_a_description_that_already_fits_completely_alone():
    verdict = grade(author="dependabot[bot]")
    assert not verdict["description"].endswith("...")
    assert verdict["description"].endswith("by design.")


# --------------------------------------------------------------------------
# The workflows -- these need no jq, but share the module-level skip.
# --------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _run_blocks(doc: dict) -> list[str]:
    blocks = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            if "run" in step:
                blocks.append(step["run"])
    return blocks


def _triggers(path: Path) -> dict:
    """YAML 1.1 parses a bare ``on:`` key as the boolean ``True``."""
    doc = _load(path)
    return doc.get("on") or doc[True]


def test_publishes_to_the_agreed_status_context():
    assert 'context="dispatch/require-review"' in STATUS_WORKFLOW.read_text()


def test_posts_the_status_to_the_resolved_head_sha():
    body = STATUS_WORKFLOW.read_text()
    assert "statuses/${SHA}" in body
    assert "sha=${sha}" in body


def test_listens_for_edited_comments_not_only_created_ones():
    """The reviewer's tracking comment is created unticked and EDITED to
    completion, so on ``created`` alone the gate never sees a run finish."""
    types = _triggers(STATUS_WORKFLOW)["issue_comment"]["types"]
    assert set(types) == {"created", "edited"}


def test_re_evaluates_when_a_formal_review_is_submitted():
    assert _triggers(STATUS_WORKFLOW)["pull_request_review"]["types"] == ["submitted"]


def test_both_workflows_queue_concurrent_runs_instead_of_cancelling_them():
    for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW):
        assert _load(path)["concurrency"]["cancel-in-progress"] is False, path.name


def test_publishes_pending_not_failure_when_the_reviewer_token_is_unset():
    """The repo has no ``CLAUDE_CODE_OAUTH_TOKEN``. A red X nobody but Tom can
    clear is the workload transfer this rollout was chosen to avoid."""
    lines = STATUS_WORKFLOW.read_text().splitlines()
    # Anchored on the CONDITION and not on the whole `if` line: the branch is
    # now guarded by `[ "$state" != "success" ] &&` as well, because it used to
    # pre-empt the verdict entirely (see
    # `test_the_unprovisioned_check_runs_after_the_verdict_not_before_it`).
    start = next(i for i, ln in enumerate(lines) if '[ -z "${REVIEWER_TOKEN}" ]; then' in ln)
    # Line boundaries, not `split("fi")`: that is a SUBSTRING split and it cut
    # at the "fi" inside the word "first" in a comment, silently dropping the
    # one line this test exists to assert on.
    end = next(i for i, ln in enumerate(lines[start + 1 :], start + 1) if ln.strip() == "fi")
    unset_branch = "\n".join(lines[start + 1 : end])
    assert "publish pending" in unset_branch
    assert "publish failure" not in unset_branch
    assert "CLAUDE_CODE_OAUTH_TOKEN" in unset_branch


def test_skips_cross_repository_prs_and_the_branches_are_not_invertible_silently():
    body = STATUS_WORKFLOW.read_text()
    assert 'if [ "$head_repo" = "$REPO" ]' in body
    assert "if: steps.fork.outputs.fork != 'true'" in body


def test_publishes_an_error_status_rather_than_nothing_when_evaluation_fails():
    """A gate's failure mode must not be "leave the last verdict standing"."""
    assert "trap 'publish error" in STATUS_WORKFLOW.read_text()


def test_reassembles_every_paginated_list_response_into_one_document():
    """``--paginate`` without ``--jq`` emits one array PER PAGE, which is not a
    single JSON document."""
    body = STATUS_WORKFLOW.read_text()
    for endpoint in ("issues/${NUMBER}/comments", "pulls/${NUMBER}/reviews"):
        line = next(ln for ln in body.splitlines() if endpoint in ln)
        assert "--paginate --jq '.[]'" in line
        assert "jq -s '.'" in line


def test_runs_the_committed_jq_program_rather_than_an_inline_copy_of_it():
    body = STATUS_WORKFLOW.read_text()
    assert "jq -f .github/review-gate.jq" in body
    assert (
        _load(STATUS_WORKFLOW)["jobs"]["require-review"]["steps"][0]["with"]["sparse-checkout"]
        == ".github/review-gate.jq"
    )


def test_no_github_expression_is_interpolated_into_any_run_block():
    """An expression in a ``run:`` body is evaluated before the shell sees it,
    even inside a ``#`` comment -- so a malformed one invalidates the whole
    workflow file, and a well-formed one splicing a PR title is a command
    injection. Every value must arrive through ``env:``.
    """
    seen = 0
    for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW):
        for block in _run_blocks(_load(path)):
            seen += 1
            assert "${{" not in block, f"{path.name}: expression inside a run: block"
    # Floor. An absence sweep over an empty collection is a tautology.
    assert seen == 6, f"expected 6 run: blocks across the two workflows, saw {seen}"


def test_the_reviewer_skips_exactly_one_title_prefix_and_the_gate_exempts_none():
    """If a ``docs`` prefix is ever added over there, this goes red -- rather
    than silently creating a PR shape the reviewer declines and the gate
    demands."""
    condition = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]["if"]
    prefixes = re.findall(r"startsWith\(github\.event\.pull_request\.title,\s*'([^']+)'\)", condition)
    assert prefixes == ["chore"]
    assert "title" not in GATE.read_text().split("def exempt_reason:", 1)[1].split(";", 1)[0]


def test_the_author_exemptions_agree_between_the_two_files():
    condition = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]["if"]
    reviewer_skips = set(re.findall(r"user\.login != '([^']+)'", condition))
    gate_body = GATE.read_text().split("def exempt_reason:", 1)[1].split(";", 1)[0]
    gate_exempts = set(re.findall(r'\.author == "([^"]+)"', gate_body))
    assert reviewer_skips == gate_exempts == {"dependabot[bot]"}


def _reviewer_action() -> dict:
    job = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]
    return next(s for s in job["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action@"))


def test_the_reviewer_carries_all_five_config_layers():
    """Each layer fails quietly and only the next one's failure appears once
    the prior is fixed, so they are asserted together rather than discovered
    one CI run at a time."""
    job = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]
    action = _reviewer_action()
    assert job["permissions"]["id-token"] == "write"  # layer 1
    assert "mcp__github_inline_comment__create_inline_comment" in action["with"]["claude_args"]  # layer 2
    assert action["with"]["track_progress"] is True  # layer 3
    assert action["with"]["allowed_bots"] == "metaframework-dispatch-bot"  # layer 4
    assert action["with"]["classify_inline_comments"] is False
    # Layer 0 is the secret gate, which is a step rather than an input.
    assert any(s.get("id") == "gate" for s in job["steps"])
    assert all(s.get("if") == "steps.gate.outputs.ready == 'yes'" for s in job["steps"] if s.get("id") != "gate")


def test_every_third_party_action_is_pinned_to_a_sha():
    seen = 0
    for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW):
        for job in _load(path).get("jobs", {}).values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    seen += 1
                    assert re.search(r"@[0-9a-f]{40}$", uses), f"{path.name}: {uses} is not SHA-pinned"
    # Floor. Without it this passes on a reader that found no `uses:` at all --
    # a renamed key or a restructured document makes the sweep vacuous rather
    # than red. Two actions in the reviewer, one in the status workflow.
    assert seen == 3, f"expected 3 pinned actions across the two workflows, saw {seen}"


def test_the_reviewer_keys_the_fork_skip_on_full_name_not_on_the_fork_flag():
    """``head.repo.fork`` is the wrong spelling, and here it would pass anyway.

    It answers "is the head repo a fork of something" -- a property of the
    REPO. ``tclancy/abi-django-utils`` is not itself a fork (measured
    2026-10-25), so the wrong spelling behaves correctly in this repo today and
    would surface only when the file is copied onward. On the sibling repo that
    shipped this gate first it was load-bearing: ``tclancy/bluelinky`` IS a
    fork, so that field is ``true`` for every same-repo branch and the wrong
    spelling skipped 100% of its PRs while reading as an installed gate.

    Comments are stripped before the absence check, because both files discuss
    ``head.repo.fork`` in prose at length -- the control below is what proves
    the stripping left something to read.
    """
    for path in (REVIEWER_WORKFLOW, STATUS_WORKFLOW):
        code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
        assert "head.repo.full_name" in code, path.name
        assert "head.repo.fork" not in code, f"{path.name}: keyed on head.repo.fork, which is a repo property"


def test_control_both_files_do_discuss_the_fork_flag_in_prose():
    """Reachability control for the absence assertion above.

    If the warning were ever deleted, the absence check would pass on a file
    that no longer explains why the spelling matters -- and the next person to
    copy it has nothing to read. This asserts the comment-stripped and
    comment-included views genuinely differ.
    """
    for path in (REVIEWER_WORKFLOW, STATUS_WORKFLOW):
        assert "head.repo.fork" in path.read_text(), f"{path.name}: the warning prose is gone"


def test_the_secret_gate_actually_tests_the_secret():
    """Layer 0, and the live failure if it is neutered.

    Replacing the condition with a constant makes the action run with an empty
    credential on every PR and fail its own validation -- a red X on every PR
    here, which is exactly what shipping the status half was chosen to avoid.
    """
    job = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]
    gate = next(s for s in job["steps"] if s.get("id") == "gate")
    assert gate["env"]["REVIEWER_TOKEN"].strip() == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert 'if [ -n "${REVIEWER_TOKEN}" ]; then' in gate["run"]
    assert "ready=yes" in gate["run"]
    assert "ready=no" in gate["run"]


def test_the_reviewer_turn_budget_is_pinned_to_the_measured_value():
    """40 is itguy's measurement plus headroom (32 turns observed). A cap sized
    to a broken gate starves a healthy one: three of itguy's five runs under 25
    completed a real review and were failed anyway by the action's post-check.
    """
    action = _reviewer_action()
    assert "--max-turns 40" in action["with"]["claude_args"]


def test_the_review_trigger_does_not_fire_on_a_quoted_review_url():
    """`startsWith`, not `contains`.

    A comment quoting `repos/tclancy/abi-django-utils/pulls/5/reviews` contains
    the substring `/review`, and under `contains` would spawn a full model
    review nobody asked for -- the same burn `synchronize` was turned off to
    avoid. The gate's own refusal text says "Comment /review", which is the
    spelling `startsWith` accepts.
    """
    condition = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]["if"]
    assert "startsWith(github.event.comment.body, '/review')" in condition
    assert "contains(github.event.comment.body, '/review')" not in condition


def test_the_reviewer_does_not_auto_trigger_on_every_push():
    """metaframework#320 section 5. With ``synchronize`` on, a 10-commit PR
    fires 10 model reviews; ``reopened`` is the deliberate one-per-re-gate
    trigger that replaces it."""
    assert _triggers(REVIEWER_WORKFLOW)["pull_request"]["types"] == ["opened", "reopened"]


def test_every_run_block_is_valid_shell():
    """``bash -n`` over each extracted block.

    A syntax error in the status workflow's third step kills it BEFORE the
    ``trap`` is installed, so the gate publishes nothing and the
    "never leave the last verdict standing" invariant is void -- while every
    other test in this module stays green.
    """
    blocks = [b for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW) for b in _run_blocks(_load(path))]
    # Floor: the reader must actually be finding blocks.
    assert len(blocks) == 6, f"expected 6 run: blocks across the two workflows, saw {len(blocks)}"
    for block in blocks:
        subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True, check=True)


def test_control_bash_n_still_rejects_broken_shell():
    """Reachability control for the sweep above: if ``bash -n`` ever stopped
    refusing, that sweep would pass on anything."""
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            ["bash", "-n"], input='if [ -z "$X" ; then echo oops', text=True, capture_output=True, check=True
        )


# ``test_a_failed_resolution_step_still_publishes_a_verdict`` used to live
# here. It asserted ``"steps.pr.outputs.sha" in final["if"]`` -- that is, it
# PINNED the condition that disarmed the fallback in the one case the fallback
# was added for. It is replaced by the three ``test_the_failure_fallback_*``
# tests at the end of this module, which assert the invariant instead of the
# implementation that broke it. Worth keeping the note: this suite was green,
# and green over a guard it was holding in place upside-down.


# ==========================================================================
# Ported back from the three downstream copies (bluelinky PR #53,
# parsons-remote PR #26, lintarr PR #49)
#
# This file is the FIRST copy of this gate (2026-10-25). Three more were cut
# from it, each one got its own review round, and each round found something
# real that was never brought home. Four of those findings are below. Nothing
# here is a new idea; every one was measured against the committed program in
# this repo before being fixed, and the reproduction is in the docstring.
#
# The direction that matters: finding A is a FALSE GREEN on a public repo, and
# findings B and D are the ones nothing in either suite could see.
# ==========================================================================


def association_for(login: str) -> str:
    """The ``author_association`` GitHub really sends for ``login``.

    ``review()`` above omits this field, and every real GitHub review object
    carries it. That omission is not cosmetic: measured on bluelinky PR #53,
    adding ``or (.author_association != null)`` to the reviews arm passed all
    of that repo's tests while a production-shaped payload went straight back
    to the bug the arm had just been fixed for. A fixture that omits a field
    cannot contest a rule that reads it.

    Derived from the login rather than hardcoded, which is parsons-remote PR
    #26's correction to the first version of this fix. Defaulting every fixture
    to the adversarial ``"NONE"`` makes the Tom-shaped success cases assert an
    OWNER-with-``NONE`` payload GitHub cannot send; under a plausible narrowing
    of the gate that turns six tests red for the wrong reason.
    """
    return "OWNER" if login == "tclancy" else "NONE"


def real_review(state: str, login: str = "tclancy", submitted_at: str | None = AFTER_HEAD) -> dict:
    """``review()`` plus the fields a real GitHub review object always has."""
    obj = review(state, login=login, submitted_at=submitted_at)
    obj["author_association"] = association_for(login)
    return obj


# --------------------------------------------------------------------------
# A. The formal-review arm accepted ANY login (false green, public repo)
# --------------------------------------------------------------------------


def test_a_stranger_cannot_turn_the_gate_green_with_a_drive_by_review():
    """Reproduced against the committed program before the fix, 2026-11-02::

        reviews: [{user: {login: "random-passerby"},
                   state: "CHANGES_REQUESTED"}]
          -> {"state": "success",
              "description": "Review present: formal CHANGES_REQUESTED by ..."}

    This repo is PUBLIC, so any authenticated GitHub account may submit a
    review on an open PR. Before the fix, doing so turned ``dispatch/
    require-review`` green having read nothing -- and CHANGES_REQUESTED, the
    *most* adversarial verdict, worked as well as an approval, because the arm
    filtered on the review's STATE and never on its author.
    """
    verdict = grade(reviews=[real_review("CHANGES_REQUESTED", login="random-passerby")])
    assert verdict["state"] == "failure", verdict


def test_one_fleet_bot_cannot_satisfy_the_gate_on_another_bots_pr():
    """The threat model this gate exists for, not a hypothetical stranger.

    GitHub permits one App's installation to submit a formal review on a PR
    opened by another, so ``metaframework-dispatch-bot[bot]`` approving a
    ``dispatch-bot[bot]`` PR satisfied the arm. That is the same one-command
    self-service bypass ``review-gate.jq`` refuses to grant ``chore:`` titles.
    """
    verdict = grade(
        author="dispatch-bot[bot]",
        reviews=[real_review("APPROVED", login="metaframework-dispatch-bot[bot]")],
    )
    assert verdict["state"] == "failure", verdict


def test_toms_own_formal_review_still_satisfies_the_gate():
    """The arm's whole purpose: with no Claude App installed on this repo it is
    currently the ONLY arm that can ever go green, so narrowing it wrongly
    makes the gate unsatisfiable by anybody."""
    for state in ("APPROVED", "CHANGES_REQUESTED"):
        verdict = grade(reviews=[real_review(state, login="tclancy")])
        assert verdict["state"] == "success", (state, verdict)


def test_the_human_allow_list_is_configurable_and_actually_read():
    """Control for the two tests above: they must fail because of the LOGIN,
    not because the arm was disabled outright. Hand the gate a different
    allow-list and the same payload passes."""
    payload = {"reviews": [real_review("APPROVED", login="random-passerby")]}
    assert grade(**payload)["state"] == "failure"
    assert grade(reviewer_logins=["random-passerby"], **payload)["state"] == "success"


def test_the_two_allow_lists_answer_different_questions_and_are_not_merged():
    """``bot_logins`` and ``reviewer_logins`` must not be collapsed into one.

    A review BOT satisfies the gate by posting a COMMENT -- that is the whole
    mechanism. It must not also satisfy it by submitting a formal verdict,
    because a bot can approve its own fleet's PRs. Symmetrically, a human's
    comment is not a review.
    """
    bot_formal = grade(reviews=[real_review("APPROVED", login="claude[bot]")])
    assert bot_formal["state"] == "failure", bot_formal

    human_comment = grade(comments=[comment("Looks fine to me.", login="tclancy")])
    assert human_comment["state"] == "failure", human_comment


def test_a_fresh_stranger_review_does_not_mask_a_genuinely_absent_one():
    """The refusal must still read "no review", not "stale review".

    The stranger's review is *fresh*, so a fix that filtered it out of the
    freshness pass but left it in ``$all`` would send the reader to the stale
    branch and tell them to ``/review`` a head that has never been reviewed at
    all. Both refusals are remediable, but they name different fixes.
    """
    verdict = grade(reviews=[real_review("APPROVED", login="random-passerby")])
    assert "No code review for this head" in verdict["description"], verdict


# --------------------------------------------------------------------------
# B. The provisioning check sat UPSTREAM of the verdict
# --------------------------------------------------------------------------


def _publish_step() -> dict:
    steps = _load(STATUS_WORKFLOW)["jobs"]["require-review"]["steps"]
    return next(s for s in steps if "Evaluate the gate" in s.get("name", ""))


def test_the_unprovisioned_check_runs_after_the_verdict_not_before_it():
    """Found on parsons-remote PR #26; this repo has the pre-fix ordering.

    A Dependabot ``pull_request`` event is served from GitHub's SEPARATE
    Dependabot secret store, so ``secrets.CLAUDE_CODE_OAUTH_TOKEN``
    interpolates to the empty string on it even once Tom has added the secret.
    With the ``[ -z "${REVIEWER_TOKEN}" ]`` early exit ahead of the evaluation,
    every Dependabot PR therefore published ``pending`` with the description
    "add the CLAUDE_CODE_OAUTH_TOKEN repo secret" -- about a secret that
    exists, naming a remedy that changes nothing -- instead of the
    ``success``/"Review not required: Dependabot PR" the gate's own
    ``exempt_reason`` returns for it. This repo HAS a ``.github/
    dependabot.yml``, so that is a live shape and not a hypothetical.

    The general rule, and it is the one worth keeping: an exemption must never
    sit downstream of a provisioning check.
    """
    body = _publish_step()["run"]
    guard = 'if [ "$state" != "success" ] && [ -z "${REVIEWER_TOKEN}" ]; then'
    assert guard in body, (
        "the unprovisioned branch does not read the verdict, so it still "
        "pre-empts every exemption and every real review"
    )
    lines = [ln.strip() for ln in body.splitlines()]
    assert lines.index(guard) > lines.index('verdict="$(jq -f .github/review-gate.jq gate-input.json)"'), (
        "the provisioning check is still upstream of the evaluation"
    )


def test_an_unprovisioned_repo_with_a_real_review_still_reads_green():
    """The other half of the same defect, and the one that would have bitten
    Tom rather than Dependabot: his own formal APPROVED arrives as
    ``pull_request_review``. Were the token invisible to that event, the old
    ordering published ``pending`` over a review that had actually happened.

    Asserted on the shape of the shell rather than on the jq, because the
    ordering IS the behaviour here: ``pending`` is now reachable only when the
    verdict is not already ``success``.
    """
    # Comment lines stripped FIRST. The raw-text version of this assertion
    # failed on the paragraph above the branch, which quotes the `exit 0` it
    # describes removing -- an absence assertion over raw text cannot tell a
    # statement from a sentence about that statement, and this step's body is
    # mostly prose.
    body = "\n".join(ln for ln in _publish_step()["run"].splitlines() if not ln.lstrip().startswith("#"))
    guard = 'if [ "$state" != "success" ] && [ -z "${REVIEWER_TOKEN}" ]; then'
    unprovisioned = body.split(guard, 1)
    assert len(unprovisioned) == 2, "no verdict-aware unprovisioned branch to assert on"
    before = unprovisioned[0]
    assert "exit 0" not in before, "something still short-circuits the step before the verdict is computed"
    assert "review-gate.jq gate-input.json" in before, (
        "the control for this test: the evaluation really is upstream of the guard"
    )


def test_the_unprovisioned_description_names_the_dependabot_secret_store():
    """The description a reader acts on. Naming the separate store is what
    stops the next person re-adding a secret that is already there."""
    lines = _publish_step()["run"].splitlines()
    # Line boundaries, not `split("fi")`. That is a SUBSTRING split, and the
    # test immediately above this block in the file records it cutting at the
    # "fi" inside a word in a comment and silently dropping the line it was
    # asserting on. The same trap, twenty lines apart.
    start = next(i for i, ln in enumerate(lines) if '[ -z "${REVIEWER_TOKEN}" ]; then' in ln)
    end = next(i for i, ln in enumerate(lines[start + 1 :], start + 1) if ln.strip() == "fi")
    branch = "\n".join(lines[start + 1 : end])
    assert "publish pending" in branch
    assert "publish failure" not in branch
    assert "Dependabot" in branch, branch


# --------------------------------------------------------------------------
# C. The failure fallback was disarmed by exactly the step it covered
# --------------------------------------------------------------------------


def test_the_failure_fallback_is_not_gated_on_the_output_of_the_step_it_covers():
    """``if: failure() && steps.pr.outputs.sha != ''`` was the pre-fix
    condition, and *Resolve PR number and head SHA* is the step whose failure
    this fallback was added for. When that step dies its output is empty, so
    the condition is false and the fallback is skipped -- the one case it
    exists to cover. The invariant it states ("a gate failure must not leave
    the last verdict standing") was therefore false of the whole job while the
    comment above it said otherwise.

    Note which direction this is wrong in: on a re-evaluation of an
    already-green head -- a ``/review`` comment, a dismissed review -- nothing
    is published and the previous ``success`` stands.
    """
    final = _load(STATUS_WORKFLOW)["jobs"]["require-review"]["steps"][-1]
    assert "failure()" in final["if"]
    assert "steps.pr.outputs.sha" not in final["if"], (
        "the fallback is still skipped when the SHA-resolution step is the one "
        "that failed, which is the case it was added for"
    )
    assert "steps.fork.outputs.fork != 'true'" in final["if"], (
        "a fork PR's token cannot write a commit status, so attempting one is a guaranteed 403 in the log"
    )


def test_the_failure_fallback_resolves_a_head_sha_for_itself():
    """Having dropped the guard, the step must do the work the guard stood in
    for: find a SHA to post to, from the event payload where there is one and
    from the API otherwise, and say so rather than dying if it cannot."""
    final = _load(STATUS_WORKFLOW)["jobs"]["require-review"]["steps"][-1]
    body = final["run"]
    assert "PR_HEAD_SHA" in final.get("env", {}) or "PR_HEAD_SHA" in body
    assert "gh api" in body, "no fallback resolution for the issue_comment trigger"
    assert "::warning::" in body, "an unresolvable SHA must be reported, not silent"
    assert "set -euo pipefail" not in body, "a fallback step that aborts on its own first error is not a fallback"


def test_the_failure_fallback_still_publishes_an_error_state():
    final = _load(STATUS_WORKFLOW)["jobs"]["require-review"]["steps"][-1]
    assert "-f state=error" in final["run"] or '-f state="error"' in final["run"]
    assert 'context="dispatch/require-review"' in final["run"]


# --------------------------------------------------------------------------
# D. The input builder was executed by nothing
# --------------------------------------------------------------------------


def active_lines(text: str) -> str:
    """``text`` with comment lines stripped, for assertions about what it DOES.

    Two jobs here. An absence assertion over raw text cannot tell a setting
    from a paragraph explaining why that setting is wrong, and these files
    carry long comments that name the wrong spellings on purpose. And the
    ``jq -n`` program below has a ``#`` comment INSIDE it -- jq would accept it,
    but a commented-out mapping has to read as missing rather than as present.

    Driven to a failure in ``test_control_active_lines_really_removes_a_line``.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_control_active_lines_really_removes_a_line():
    assert "bot_logins" not in active_lines("          # bot_logins: [...]\n")
    assert "bot_logins" in active_lines('          bot_logins: ["claude[bot]"],\n')


def test_control_active_lines_alters_nothing_but_whole_comment_lines():
    """Bounds the helper over the real files: the only lines it may change are
    ones whose first non-space character is ``#``."""
    for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW, GATE):
        raw = path.read_text().splitlines()
        kept = active_lines(path.read_text()).splitlines()
        assert kept == [ln for ln in raw if not ln.lstrip().startswith("#")], path.name


class TestTheInputBuilderIsExecutedAndNotOnlyRead:
    """The gate's ``jq -n --slurpfile ...`` program, run rather than grepped.

    THIS WAS THE WIDEST HOLE IN THIS FILE, and the three repos cut from it
    inherited it -- lintarr PR #49 is where it was finally measured. The
    verdict tests above feed ``review-gate.jq`` a HAND-WRITTEN document. The
    real document is assembled by the program in ``require-review.yml``'s
    publish step, and nothing in this suite ran that program. A pure-function
    test proves the function, not the caller that builds its input.

    Measured on lintarr: five one-token mutations to that program survived the
    entire suite, and each is a production-visible failure --

      ``bot_logins: ["nobody[bot]"]``       every reviewed PR reads red forever
      ``reviewer_logins: ["someone-else"]`` the gate becomes unsatisfiable, and
                                            on this repo that arm is currently
                                            the ONLY one that can go green
      ``author: $pr[0].head.user.login``    ``exempt_reason`` stops matching, so
                                            every Dependabot PR reads red
      ``head_committed_at: ...author.date`` on a rebased head the author date is
                                            older, so a review that predates the
                                            push reads FRESH -- a false green
      ``number: $pr[0].id``                 the refusal names a PR that does not
                                            exist

    So: extract the program, run it against fixtures, and assert the MAPPING --
    with two differing rows, because a field asserted to hold *a* legal value
    survives both a hardcoded constant and a read of the wrong row.
    """

    #: Where each output key must come from. The whole point of the table: a
    #: guard that only checked the keys exist would survive every row above.
    EXPECTED_SOURCES = {
        "number": ("pr", "number"),
        "author": ("pr", "user.login"),
        "title": ("pr", "title"),
        "head_committed_at": ("head", "commit.committer.date"),
    }

    @staticmethod
    def _program() -> str:
        """The ``jq -n ... '<program>'`` text from the publish step."""
        active = active_lines(STATUS_WORKFLOW.read_text())
        anchor = "jq -n \\"
        assert anchor in active, "no `jq -n` input builder in the workflow"
        tail = active[active.index(anchor) :]
        start = tail.index("'{")
        end = tail.index("}'", start)
        return tail[start + 1 : end + 1]

    def test_the_program_slice_is_a_program_and_not_the_step(self):
        # Control for the slice: one that over-reached would hand jq the
        # surrounding shell and every assertion below would fail for the wrong
        # reason, or -- with a laxer slice -- succeed on a fragment.
        program = self._program()
        assert program.startswith("{") and program.rstrip().endswith("}")
        assert "gh api" not in program, "the slice swept in the shell around the program"
        assert "slurpfile" not in program
        for key in self.EXPECTED_SOURCES:
            assert f"{key}:" in program, f"{key} missing from the extracted program"

    @staticmethod
    def _row(n: int) -> dict:
        """Two rows that differ in EVERY field the builder reads.

        ``n`` is 1 or 2. Nothing is shared between them, so a builder that
        hardcodes a value, or reads the right field off the wrong document,
        produces row 1's answer for row 2's input and is caught.
        """
        return {
            "pr": {
                "number": 100 + n,
                "user": {"login": f"author-{n}"},
                "title": f"feat: change {n}",
                # A decoy at `head.user.login`: one of the five mutations read
                # the HEAD repo's owner instead of the PR author, and with no
                # such field present that mutant yields null and could look
                # like a different bug.
                "head": {"user": {"login": f"head-owner-{n}"}},
            },
            "head": {
                "commit": {
                    "committer": {"date": f"2026-10-2{n}T04:00:00Z"},
                    # An author date OLDER than the committer date is the real
                    # shape after a rebase, which is the case where reading the
                    # wrong one is a false green rather than a wash.
                    "author": {"date": f"2026-10-0{n}T04:00:00Z"},
                }
            },
            "comments": [{"user": {"login": f"commenter-{n}"}, "body": f"c{n}"}],
            "reviews": [{"user": {"login": f"reviewer-{n}"}, "state": "APPROVED"}],
        }

    def _build(self, row: dict, tmp_path: Path) -> dict:
        """Run the real program over ``row``, exactly as the workflow does."""
        if JQ is None:
            pytest.skip("jq is not installed")
        args = []
        for name in ("pr", "head", "comments", "reviews"):
            path = tmp_path / f"{name}.json"
            path.write_text(json.dumps(row[name]))
            args += ["--slurpfile", name, str(path)]
        proc = subprocess.run(
            [JQ, "-n", *args, self._program()],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"the input builder failed:\n{proc.stderr}"
        return json.loads(proc.stdout)

    def test_the_builder_produces_exactly_the_keys_the_gate_reads(self, tmp_path):
        assert set(self._build(self._row(1), tmp_path)) == {
            "number",
            "author",
            "title",
            "head_committed_at",
            "bot_logins",
            "reviewer_logins",
            "comments",
            "reviews",
        }

    @pytest.mark.parametrize("key", sorted(EXPECTED_SOURCES))
    def test_each_field_is_read_from_the_document_and_path_it_names(self, tmp_path, key):
        """Two rows, so membership cannot stand in for correspondence."""
        doc, path = self.EXPECTED_SOURCES[key]
        for n in (1, 2):
            row = self._row(n)
            want = row[doc]
            for part in path.split("."):
                want = want[part]
            got = self._build(row, tmp_path)[key]
            assert got == want, (
                f"{key} came back {got!r} but {doc}.{path} is {want!r} on row {n} -- "
                "the builder is reading a different field, a different document, "
                "or a hardcoded value"
            )

    def test_the_head_timestamp_is_the_committer_date_and_not_the_author_date(self, tmp_path):
        # Called out separately because it is the only one of the five whose
        # wrong answer is a FALSE GREEN rather than a false red: after a rebase
        # or an amend the author date is older than the push, so a stale review
        # would compare fresh against it.
        row = self._row(1)
        built = self._build(row, tmp_path)
        assert built["head_committed_at"] == row["head"]["commit"]["committer"]["date"]
        assert built["head_committed_at"] != row["head"]["commit"]["author"]["date"]

    def test_the_author_is_the_pr_author_and_not_the_head_repo_owner(self, tmp_path):
        # The decoy row exists so this can distinguish "reads the right field"
        # from "reads a field that happens to be absent".
        row = self._row(1)
        built = self._build(row, tmp_path)
        assert built["author"] == row["pr"]["user"]["login"]
        assert built["author"] != row["pr"]["head"]["user"]["login"]

    def test_the_two_allow_lists_are_the_ones_the_gate_and_the_reviewer_agree_on(self, tmp_path):
        """The literals, read out of the BUILT document rather than grepped.

        A ``"claude[bot]" in STATUS_WORKFLOW.read_text()`` assertion cannot see
        either of these go wrong: that literal appears five times in this
        workflow and seven in ``review-gate.jq``, nearly all of them in prose.
        """
        built = self._build(self._row(1), tmp_path)
        assert built["bot_logins"] == ["claude[bot]"], (
            "the gate would trust a login the reviewer never posts as, so every reviewed PR would read red"
        )
        assert built["reviewer_logins"] == ["tclancy"], (
            "with no Claude App installed here this is the only arm that can go "
            "green, so a wrong login makes the gate unsatisfiable by anyone"
        )

    def test_the_collections_pass_through_unflattened(self, tmp_path):
        # `--slurpfile` makes each `$name` an ARRAY of the file's documents, so
        # every reference needs `[0]`. Dropping it on `comments` yields a
        # list-of-lists, which `review-gate.jq`'s `.comments[]?` then iterates
        # into arrays and silently matches nothing.
        row = self._row(2)
        built = self._build(row, tmp_path)
        assert built["comments"] == row["comments"]
        assert built["reviews"] == row["reviews"]

    def test_the_built_document_actually_satisfies_the_gate_end_to_end(self, tmp_path):
        """The two halves joined: builder output straight into the real gate.

        Neither half's tests covered this seam. A builder that is individually
        correct and a gate that is individually correct can still disagree
        about a key name, and the symptom would be a permanently red status
        with no explanation in either file.
        """
        if JQ is None:
            pytest.skip("jq is not installed")
        row = self._row(1)
        row["reviews"] = [
            {
                "user": {"login": "tclancy"},
                "state": "APPROVED",
                "submitted_at": "2026-10-29T04:00:00Z",
                "author_association": "OWNER",
            }
        ]
        built = self._build(row, tmp_path)
        proc = subprocess.run(
            [JQ, "-f", str(GATE)],
            input=json.dumps(built),
            capture_output=True,
            text=True,
            check=True,
        )
        verdict = json.loads(proc.stdout)
        assert verdict["state"] == "success", verdict
        assert "tclancy" in verdict["description"]

    def test_control_the_end_to_end_seam_can_fail(self, tmp_path):
        """Same seam, a payload with no review: the two halves must agree on a
        refusal too, and the PR number must survive into its text -- which is
        what catches ``number: $pr[0].id``."""
        if JQ is None:
            pytest.skip("jq is not installed")
        row = self._row(2)
        row["comments"] = []
        row["reviews"] = []
        built = self._build(row, tmp_path)
        proc = subprocess.run(
            [JQ, "-f", str(GATE)],
            input=json.dumps(built),
            capture_output=True,
            text=True,
            check=True,
        )
        verdict = json.loads(proc.stdout)
        assert verdict["state"] == "failure", verdict
        assert "#102" in verdict["description"], verdict
