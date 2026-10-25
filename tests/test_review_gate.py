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


def test_jq_is_available_wherever_this_suite_is_graded():
    """Reachability control for the whole module.

    Everything below is skipped when ``jq`` is absent, and a skipped gate test
    reads exactly like a passing one. That trade is fine on a laptop and is not
    fine in CI, which is the only run anyone grades a PR on -- so in CI a
    missing ``jq`` is a failure, not a skip. ``ubuntu-latest`` ships jq, so
    this asserts a property of the runner rather than adding a dependency.
    """
    if os.environ.get("CI"):
        assert JQ is not None, "jq is missing from the CI runner; every gate test below silently skipped"
    else:
        pytest.skip("not CI; the jq-driven tests skip on their own if jq is absent")


pytestmark = pytest.mark.skipif(JQ is None, reason="jq is not installed")


def grade(**overrides) -> dict:
    """Run the committed jq program over one gate input and return the verdict."""
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
    body = STATUS_WORKFLOW.read_text()
    unset_branch = body.split('if [ -z "${REVIEWER_TOKEN}" ]; then', 1)[1].split("fi", 1)[0]
    assert "publish pending" in unset_branch
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
    for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW):
        for block in _run_blocks(_load(path)):
            assert "${{" not in block, f"{path.name}: expression inside a run: block"


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


def test_the_reviewer_carries_all_five_config_layers():
    """Each layer fails quietly and only the next one's failure appears once
    the prior is fixed, so they are asserted together rather than discovered
    one CI run at a time."""
    job = _load(REVIEWER_WORKFLOW)["jobs"]["claude-review"]
    action = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action@"))
    assert job["permissions"]["id-token"] == "write"  # layer 1
    assert "mcp__github_inline_comment__create_inline_comment" in action["with"]["claude_args"]  # layer 2
    assert action["with"]["track_progress"] is True  # layer 3
    assert action["with"]["allowed_bots"] == "metaframework-dispatch-bot"  # layer 4
    assert action["with"]["classify_inline_comments"] is False
    # Layer 0 is the secret gate, which is a step rather than an input.
    assert any(s.get("id") == "gate" for s in job["steps"])
    assert all(s.get("if") == "steps.gate.outputs.ready == 'yes'" for s in job["steps"] if s.get("id") != "gate")


def test_every_third_party_action_is_pinned_to_a_sha():
    for path in (STATUS_WORKFLOW, REVIEWER_WORKFLOW):
        for job in _load(path).get("jobs", {}).values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    assert re.search(r"@[0-9a-f]{40}$", uses), f"{path.name}: {uses} is not SHA-pinned"
