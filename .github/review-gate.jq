# Review-gate decision logic for `require-review.yml` (issue #24, option (a)
# of tclancy/bluelinky#35).
#
# A pure function: JSON in, verdict out. No network, no `gh`, no state. The
# workflow does the fetching; this file does the deciding, which is why it can
# be unit-tested against fixtures by `tests/test_review_gate.py`.
#
# Ported from `tclancy/bluelinky`'s copy (PR #52), which is the precedent for
# this rollout. It is a deliberately slim alternative to vendoring
# metaframework's 668-line `src/dispatch/review_gate.py` into a fourth repo:
# that module lives in a PRIVATE repo, so a public repo's `github.token`
# cannot read it cross-repo, and its extra sophistication is tuning for that
# repo's own history.
#
# INPUT
#   {
#     "number":            42,
#     "head_committed_at": "2026-10-25T03:00:00Z" | null,
#     "author":            "<PR author login>",
#     "title":             "<PR title>",
#     "bot_logins":        ["claude[bot]"],
#     "comments":          [ GitHub issue-comment objects ],
#     "reviews":           [ GitHub review objects ]
#   }
#
# OUTPUT
#   { "state": "success" | "failure", "description": "<=140 chars" }

# Timestamps are compared as STRINGS, not parsed. Every value here comes from
# the same GitHub REST API in the same `YYYY-MM-DDTHH:MM:SSZ` shape, and for
# two same-shaped ISO-8601 UTC stamps lexicographic order *is* chronological
# order. `fromdateiso8601` would be more obviously correct and also introduces
# a parse-failure mode (it rejects fractional seconds) into a file whose whole
# job is to return a verdict; string comparison has no failure mode at all.
#
# A null on either side means "cannot be shown to be stale", so the review
# counts -- matching review_gate.py's `_is_stale`, which refuses to invent a
# refusal out of an absent field (a PENDING review has `submitted_at: null`).
def is_fresh($head):
  if . == null or $head == null then true else . >= $head end;

# Whether a review-bot comment represents a review that actually HAPPENED.
#
# The obvious rule -- "no outstanding `- [ ]` items" -- is wrong in the one
# direction that matters. It only rejects a non-review if the non-review
# carries an UNTICKED BOX, and the two comment shapes `claude-code-action`
# emits when it has reviewed nothing carry no checklist at all:
#
#   The in-flight comment, from the pinned action's own source
#   (`src/github/operations/comments/common.ts`, `createCommentBody`).
#   `track_progress: true` forces tag mode, which posts this at PREPARE time,
#   before any review work happens:
#
#       Claude Code is working... <img .../>
#
#   The errored-run comment, measured on four real fleet comments
#   (homelab#350 three consecutive runs, homelab#351):
#
#       **Claude encountered an error** -- [View job](...)
#
#   Under the naive rule the gate publishes `success` seconds after the
#   reviewer spawns, and homelab#350 is literally a green gate built out of
#   three error messages. That is metaframework#687 -- "18 PRs passed the gate
#   with zero reviews".
#
# So the rule has two parts. An outstanding box still disqualifies anything.
# On top of that, a comment that IS one of the action's own status comments
# must say it finished, or carry a completed checklist:
#
#   shape                                        verdict  why
#   in-flight "Claude Code is working..."        no       no checklist, not finished
#   errored, no checklist (homelab#350)          no       no checklist, not finished
#   errored, partial checklist (itguy#207)       no       outstanding box
#   errored, completed checklist                 YES      keeps metaframework#560
#   "**Claude finished ... in 3m 48s**"          YES      finished (sandy#175)
#   plain summary posted via `gh pr comment`     YES      not a status comment
#   prose quoting `- [ ] ` mid-sentence          YES      marker is line-anchored
#
# The errored-but-completed row is deliberate and is NOT laxness: #560 found
# that an `error_max_turns` run has read the diff and posted its findings
# before blowing its budget, and three substantive fleet reviews carry the
# error marker. Rejecting those would silently reverse a measured decision.
#
# One consequence: the gate's green arrives on a comment EDIT (the in-flight
# comment is edited into the "finished" one), which is why `require-review.yml`
# listens for `issue_comment: [created, edited]` and scores comments on
# `updated_at`. On `created` alone the gate could never see a run finish.

# The action's own status comments, identified by their leading status line.
# `^\*{0,2}` because the in-flight line is unbolded and the other two are
# bolded, and `encountered an error` is matched as a PREFIX so it covers both
# `**Claude encountered an error**` and `**Claude encountered an error after
# 2m 10s**` (itguy#207's shape).
def is_action_status:
  test("^\\*{0,2}Claude (Code is working|finished|encountered an error)");

def says_finished: test("^\\*{0,2}Claude finished");

# `(^|\n)` rather than `\n` alone: a body whose very first bytes are a
# checklist item has no preceding newline. No real shape measured here starts
# that way, but the anchor costs nothing and the alternative is a rule that
# silently stops applying to a shape nobody has seen yet.
def has_outstanding_item: test("(^|\n)- \\[ \\] ");
def has_completed_item:   test("(^|\n)- \\[x\\] ");

def counts_as_review:
  (. != null)
  and (. != "")
  # Line-anchored on purpose: a review that *discusses* checklists in prose
  # should not be thrown away for quoting the marker mid-sentence.
  and (has_outstanding_item | not)
  and (if is_action_status
       then (says_finished or has_completed_item)
       else true end);

def truncate140:
  if length > 140 then (.[0:137] + "...") else . end;

def pass($why): { state: "success", description: ($why | truncate140) };
def fail($why): { state: "failure", description: ($why | truncate140) };

# The ONE exemption, and it is on the AUTHOR, never on a title prefix.
#
# The reviewer's `if:` declines two kinds of PR: Dependabot's, and any whose
# title starts with `chore`. The obvious move is to exempt both here so the
# gate never demands a review the reviewer declines to perform. The first is
# right; the second is not, and `review_gate.py` says why in its own comment on
# `EXEMPT_PR_AUTHORS` -- the title-prefix heuristic is the bug that constant
# replaced. It over-matches (a human `chore:` PR goes unreviewed) and
# under-matches (a hand-retitled Dependabot PR gets gated again). In a gate
# whose whole purpose is backpressure on AGENT PRs, a title prefix is a
# one-word self-service bypass: any agent can title its PR `chore:` and never
# face a reviewer.
#
# Leaving `chore` out does not create an unremediable red X, because this gate
# HAS a remedy that works without Tom: a `/review` comment runs the reviewer
# and re-publishes this status (see the refusal message below). So a `chore`
# PR reads red until someone asks for a review, which is backpressure rather
# than a dead end.
#
# `tests/test_review_gate.py` asserts the AUTHOR lists agree between the two
# files, and asserts the reviewer's title-prefix list is exactly `chore` -- so
# adding a `docs` prefix over there is red here, rather than silently creating
# the dead end this paragraph says cannot happen.
def exempt_reason:
  if .author == "dependabot[bot]"
  then "Review not required: Dependabot PR -- the reviewer skips these by design."
  else null end;

# `index`, not `inside`/`contains`: on arrays those two do SUBSTRING matching
# against each element, so `["claude"] | inside(["claude[bot]"])` is true and
# any login that is a prefix of an allow-listed one would satisfy the gate.
# Measured on jq 1.8.1: `["claude[bot]"] | index("claude")` is null, while
# `["claude[bot]"] | index(["claude[bot]"])` is 0 -- an array argument is a
# subsequence search, a scalar argument is an exact element match.
#
# `. as $l` FIRST, and that line is load-bearing. The obvious spelling,
# `($bots | index(. // ""))`, rebinds `.` to `$bots` as it crosses the pipe, so
# it evaluates `index($bots)` -- the subsequence form above -- and returns 0 for
# every input. That is a predicate that cannot fail: it admits the login
# `"totally-unrelated"`. The prefix- and substring-login cases in
# `tests/test_review_gate.py` are there to keep it caught.
#
# `index(null)` returns null rather than erroring, so a comment with no author
# needs no separate guard.
def is_bot($bots): . as $l | ($bots | index($l)) != null;

def candidates($bots):
  # Comments first, then formal reviews -- so a PR satisfied by a bot review
  # comment reports that rather than a formal review that happens to coexist.
  #
  # `updated_at`, not `created_at`: the tracking comment is created unticked
  # and EDITED to completion, so its creation time is not when the review
  # existed. The trade-off, stated: editing an old comment refreshes it. An
  # edit by the review bot is the self-heal path this gate depends on, and an
  # edit by a human to their own review comment is a deliberate act by the one
  # person the gate exists to apply backpressure for.
  [ ( .comments[]?
      | select(.user.login | is_bot($bots))
      | select(.body | counts_as_review)
      | { ts: .updated_at,
          why: "Review present: comment by \(.user.login // "?") at \(.updated_at // "?")" } ),
    ( .reviews[]?
      | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED")
      | { ts: .submitted_at,
          why: "Review present: formal \(.state) by \(.user.login // "?") at \(.submitted_at // "?")" } )
  ];

. as $pr
| ($pr.bot_logins // ["claude[bot]"]) as $bots
| $pr.head_committed_at as $head
| ($pr | exempt_reason) as $exempt
| if $exempt != null then pass($exempt)
  else
    ($pr | candidates($bots)) as $all
    | ($all | map(select(.ts | is_fresh($head)))) as $fresh
    | if ($fresh | length) > 0 then pass($fresh[0].why)
      elif ($all | length) > 0 then
        # Two refusals, because the two have different causes and the
        # description is published as the commit-status text -- the
        # highest-traffic instruction site in the gate. Collapsing them sends
        # the reader to the wrong fix.
        #
        # BOTH NAME `/review`, and that is correct here even though
        # metaframework#620 says `/review` cannot re-gate a PR. #620 is about a
        # CHECK-RUN: a check-run attaches to the SHA of its own event, so an
        # `issue_comment` run's check lands on the default branch and never
        # marks the PR head reviewed. A COMMIT STATUS attaches to whatever SHA
        # you POST it to, and `require-review.yml` resolves the PR head and
        # posts there from every trigger -- which is the entire reason this
        # gate is a status and not a check. So in this repo the `/review` chain
        # closes: the comment fires the reviewer, the reviewer posts as
        # `claude[bot]`, that comment fires this workflow, and the status on the
        # head flips.
        #
        # `gh pr close && reopen` also works and is the heavier option; it is
        # what you need if you want the `claude-review` CHECK-RUN on the head
        # too, which `/review` cannot give you.
        fail("Reviews on this PR all predate its head commit (newest \(($all | map(.ts) | max) // "?") vs head \($head // "?")). Comment /review to re-review this head.")
      else
        fail("No code review for this head. Comment /review on PR #\($pr.number // 0) to run the reviewer -- it re-publishes this status.")
      end
  end
