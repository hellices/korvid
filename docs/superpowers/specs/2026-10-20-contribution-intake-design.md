# Contribution intake: an issue first, a decision before a pull request

Date: 2026-10-20. Status: phase 1 is in #447; phase 2 is proposed and needs
the maintainer's approval. Revised the same day: the rule covers the
maintainer's own pull requests too. Revised on 2026-10-21: the decision on
an issue, a confirmed bug or an accepted feature, is recorded as
`status: accepted`, and no pull request limit is used.

## Problem

Nothing in the repository said that a pull request needs an accepted issue
behind it, or that someone has to be assigned before work starts.

- #446 was opened 3.5 hours after its author commented on #445 that they would
  like to work on it. Nobody had assigned #445, and the pull request did not
  link it, so it reads as a pull request without an issue.
- The good first issues told people to "comment here if you would like to take
  this issue", and CONTRIBUTING.md said to pick one up. Neither said to wait.
  The author did what the text invited.
- #444's claimant asked to be assigned and is still waiting, because no one
  owns that step.

The maintainer's own work was not tracked either. Of the 60 pull requests
merged from 2026-08-22 to 2026-10-18, 46 were the maintainer's, and 27 of
those closed no issue; the other 14 came from Dependabot. None of the 17
open issues had an assignee. A rule for outside pull requests alone would
leave most of the work outside the issues that milestones and release notes
are built from.

Promotion will raise the volume. GitHub reports that monthly pull request
volume grew about 3.6 times since 2023, much of it drive-by and AI-assisted
([pull request limits](https://github.blog/open-source/maintainers/how-pull-request-limits-are-cutting-down-the-noise/)).
Without a written intake rule, every unsolicited pull request becomes a
negotiation.

## Goals and non-goals

Goals:

- An outside change is discussed on its issue and decided there, as a
  confirmed bug or an accepted feature, before anyone writes it.
- Every pull request, the maintainer's included, closes an accepted issue in
  this repository that is assigned to its author. Only pull requests opened
  by Dependabot or the Relock workflow are exempt.
- An issue's state shows in GitHub's own fields: labels, milestone, assignee,
  and linked pull requests. A milestone lists everything its release ships.
- Two people do not build the same thing, and review time goes to agreed work.
- Contributors can read the rule before they start, and a decline points to it.

Non-goals:

- No merge automation of any kind (AGENTS.md).
- No limit on how many pull requests are open. The rule turns on whether a
  pull request has an accepted issue behind it, not on how many there are.
- No project board for now. At 17 open issues, the saved searches below show
  the same state without a second copy to keep in sync.
- No closing the door on outside contributions.

## Options considered

| Option | What it does | Verdict |
| --- | --- | --- |
| A. Written rule only | CONTRIBUTING.md, the forms, and the pull request template state the rule; maintainers enforce it by hand | Necessary but not enough as volume grows |
| B. Written rule and an intake check | A, plus an automated check that labels pull requests without an accepted, assigned issue | **Chosen**, in two phases |
| C. Pull requests from collaborators only | The [February 2026 setting](https://github.blog/changelog/2026-02-13-new-repository-settings-for-configuring-pull-request-access/) lets only users with write access open pull requests | Rejected as the default: every contributor would need write access to a repository that holds release and Homebrew automation. Kept as an emergency lever |

GitHub's [June 2026 setting](https://github.blog/changelog/2026-06-17-limit-open-pull-requests-for-users-without-write-access/)
that limits open pull requests for users without write access is not used:
counting pull requests does not tell agreed work from unagreed work.

GitHub has no built-in rule that a pull request must close an accepted,
assigned issue. The repository has to state it and, at scale, check it.

## Who the rule covers

| Scope | Trade-off | Verdict |
| --- | --- | --- |
| Outside contributors only; write access is exempt | No change for the maintainer, but most of the work, the maintainer's, stays outside issues and milestones | Rejected |
| Everyone links an issue; write access needs no assignment | Every change is tracked, but the assignee field stops saying who is doing what, and the check has to read each author's permission | Rejected |
| Everyone closes an accepted issue assigned to them; write access only means accepting and assigning one's own | One rule. The assignee field says who is doing what, and the check reads no permission | **Chosen** |

The cost is an issue for each of the maintainer's changes. Coding agents do
most of that work, so AGENTS.md has them open the issue first.

## Phase 1: the written rule (this pull request)

- CONTRIBUTING.md gains "From issue to pull request":
  1. open an issue first, with the forms, which label it
     `status: needs-triage`;
  2. discuss it on the issue: whether a bug is real, usually by reproducing
     it, and whether a feature fits and how it should work;
  3. wait for the decision: a maintainer confirms the bug or accepts the
     feature by replacing `status: needs-triage` with `status: accepted`, or
     closes the issue with the reason; whoever offered to make the change is
     assigned, one issue at a time until a first pull request is merged, and
     an assignment with no pull request or update for 14 days may be
     released;
  4. open the pull request with `Closes #N` at the top.

  The rule is the same for maintainers, who accept and assign issues
  themselves. A pull request that does not close an accepted issue assigned
  to its author is closed without review. It can be reopened once the issue
  is accepted and assigned. Pull requests opened by Dependabot or the Relock
  workflow are exempt, and security fixes go through SECURITY.md.
- Both issue forms add `status: needs-triage`. The bug form says a
  maintainer confirms each report, usually by reproducing it, and that a fix
  waits for confirmation and assignment. The feature form says the design is
  worked out on the issue, and that work waits for acceptance and
  assignment. Each has a checkbox for offering to make the change.
- The pull request template starts with `Closes #` and has a checklist item
  for it.
- AGENTS.md tells coding agents to find or create the issue, accepted and
  assigned to the maintainer, before they open a pull request.
- The `status: needs-triage` and `status: accepted` labels exist. The
  existing `status: tbd` and `status: backlog` mean valid work that is
  deferred, not untriaged work.
- The open good first issues (#442–#445) tell claimants to wait for
  assignment.
- `tests/test_contribution_intake.py` keeps the forms, the template, and
  CONTRIBUTING.md stating the same rule.
- #447 follows the rule itself: it closes #449.

Repository settings for the maintainer: keep requiring approval before
Actions run for first-time contributors (Settings > Actions > General).

## Issue states

Each state shows in a field GitHub already has, so no separate tracker has to
be kept in sync:

| State | What shows it |
| --- | --- |
| New, under discussion | `status: needs-triage`, added by the forms; a blank issue has no status label at all |
| Accepted: a confirmed bug or an accepted feature | `status: accepted`, and a milestone once it is scheduled |
| Valid but deferred | `status: backlog`, or `status: tbd` for work deferred from the current release; it needs `status: accepted` before a pull request |
| Open to contributors | `status: accepted` with `help wanted` or `good first issue`, and no assignee |
| In progress | An assignee, then a linked pull request |
| Declined | Closed as not planned, with the reason |
| Done | Closed by the pull request that names it in `Closes #N` |

## Maintainer routine

- Open an issue for your own work before the pull request, labelled
  `status: accepted`, assigned to yourself, and in the milestone it ships
  in. Coding agents do this for you (AGENTS.md).
- Triage weekly with
  `is:issue state:open -label:"status: accepted" -label:"status: backlog" -label:"status: tbd"`,
  which lists every open issue without a decision, blank issues included.
  Discuss each on its issue. Confirm a bug, usually by reproducing it, or
  accept a feature once its design is agreed: replace
  `status: needs-triage` with `status: accepted`, and add a milestone if it
  is scheduled, or `help wanted` or `good first issue` if it is open to
  contributors. If the reporter offered to make the change, assign it to
  them at the same time. Defer valid work with `status: backlog` instead,
  and close everything else as not planned, with the reason.
- Assign on request. Before the assignee's first merge, give them one issue at
  a time. Check assignments with
  `is:issue state:open has:assignee -linked:pr sort:updated-asc`, and release
  one after 14 days without a pull request or update.
- Find pull requests that link no issue with
  `is:pr state:open -linked:issue -author:app/dependabot`. A Relock pull
  request shows up here too, and is exempt. Link the issue in your own.
  Close anyone else's that does not close an accepted issue assigned to its
  author, with this reply:

  > Thanks for the pull request. Every pull request in korvid closes an
  > accepted issue in this repository that is assigned to its author; see
  > "From issue to pull request" in CONTRIBUTING.md. Please open or find the
  > issue and discuss the change there. Once a maintainer accepts it and
  > assigns it to you, reopen this pull request and add `Closes #N` at the
  > top.

These searches use GitHub's issue search syntax as the web interface runs
it; `has:assignee` depends on it.

## Phase 2: an intake check (proposed)

A workflow that checks each pull request against the rule, so enforcement
does not depend on a maintainer noticing:

- It is triggered by `pull_request_target` (opened, edited, reopened,
  synchronize), so it runs for first-time contributors without approval. It
  never checks out or runs pull request code, uses no third-party actions,
  and passes the untrusted title and body only through environment
  variables, never through shell interpolation.
- Permissions are `pull-requests: write` and `issues: read`, nothing else.
- It passes immediately only for a pull request opened by Dependabot or the
  Relock workflow. Dependabot's is one whose `pull_request.user.login` is
  exactly `dependabot[bot]`. The Relock workflow opens its pull request with
  `GITHUB_TOKEN`, and every workflow that does so appears as
  `github-actions[bot]`, so that login alone does not identify it: the
  exemption also needs a head branch in this repository whose name starts
  with `relock/`, as only that workflow names its branches. Pushing a branch
  here takes write access, and a person's login cannot contain `[` or `]`,
  so no one else can borrow either exemption.
- Every other author, the maintainer included, needs a linked issue, and the
  check reads no permission. Only someone with triage access or more can
  assign an issue or add `status: accepted`, so an assigned, accepted issue
  already carries a maintainer's decision. That also keeps
  `authorAssociation` and later permission changes out of the decision.
- It reads the pull request's `closingIssuesReferences` through GraphQL. It
  passes only if one linked issue meets every condition:
  - It belongs to this repository: its `repository.id` equals the pull
    request's `baseRepository.id`. A closing reference such as
    `Closes other/repo#N` can name an issue in a repository the author
    controls, where they can open, accept, and assign issues themselves.
    Comparing node IDs rather than `nameWithOwner` strings leaves no room
    for letter case or a rename to confuse the comparison.
  - It is open.
  - It is assigned to the author.
  - It is accepted: it carries `status: accepted` and not
    `status: needs-triage`. A maintainer who accepts an issue replaces one
    label with the other, so an issue with both has not finished triage, and
    a blank issue, with neither, has not been decided. A milestone,
    `help wanted`, or `status: backlog` is not a decision either. An issue
    assigned by mistake before the decision does not let a pull request
    through.
- A pass can go stale. After it, the issue can be closed, unassigned, or
  lose `status: accepted`, and none of that is a pull request event. So the
  workflow also runs on `issues` events (closed, reopened, assigned,
  unassigned, labeled, unlabeled), and re-evaluates every open pull request
  whose `closingIssuesReferences` include that issue.
- Some changes leave nothing to follow. A deleted issue drops out of every
  pull request's closing references, an issue transferred to another
  repository no longer belongs to this one, and an event that GitHub fails
  to deliver never runs the workflow. So a full pass re-evaluates every open
  pull request: on `issues` `deleted` and `transferred`, on a daily
  `schedule`, and by hand through `workflow_dispatch`, which a maintainer
  runs right before merging.
- Evaluations of one pull request run one at a time. Each evaluation job
  joins a concurrency group named after its pull request, and an issue event
  or a full pass starts one job per pull request it re-evaluates. The group
  does not cancel the run in progress, which could stop it halfway through
  an update: GitHub queues the newest run and drops any older one still
  waiting, so the last evaluation to run is the newest. Each evaluation also
  reads the pull request's closing references and the issues' state just
  before it changes the label, never from the event payload, so the last
  label written matches the current state, whatever order the events
  arrived or finished in.
- The answer lives on the pull request, never on its commit. A failing
  evaluation adds `status: needs-issue` and posts the reply above once, and
  a passing one removes the label. Two open pull requests can share a head
  commit while closing different issues; a commit status belongs to the
  commit, so one pull request's answer would overwrite the other's, but a
  label belongs to one pull request. Whatever triggered the latest
  evaluation, its label is what the maintainer who merges by hand sees: the
  live answer, not the one from when the pull request was opened. The
  workflow run itself succeeds whenever it evaluated, so its own check never
  stands in for the answer.
- Closing stays a maintainer's decision until the check has run without
  false positives for a while.
- It ships with tests in the existing workflow-contract style: trigger set,
  including the issue events, the daily schedule, and the manual run, a
  concurrency group per pull request that does not cancel a run in
  progress, permissions, no checkout, no interpolation of pull request
  fields. The decision itself is a pure function of the author's login and
  the linked issues' current state, tested case by case:
  - A pull request from `dependabot[bot]` passes with no linked issue, and
    so does one from `github-actions[bot]` whose head is a `relock/` branch
    in this repository. A `github-actions[bot]` pull request from any other
    branch gets no exemption, and neither does a person whose login merely
    contains `dependabot`.
  - Any other author with no linked issue fails, whatever their permission,
    `admin` included.
  - An open, accepted issue in this repository assigned to the author
    passes, whatever the author's permission.
  - The same issue in another repository (`Closes other/repo#N`) fails,
    however it is labelled and assigned there.
  - A closed issue fails, as does an issue assigned to someone else or to
    no one, and an issue without `status: accepted` or still labelled
    `status: needs-triage`.
  - An issue assigned to the author fails until a maintainer labels it
    `status: accepted`, whatever else it carries: a milestone,
    `help wanted`, or `status: backlog` alone does not pass.
  - One passing issue among failing ones passes.
  - A pull request that passed fails on re-evaluation once its issue is
    closed, unassigned, loses `status: accepted`, or is labelled
    `status: needs-triage` again.
  - An issue event re-evaluates exactly the open pull requests that close
    that issue, and leaves the others alone.
  - Two open pull requests that share a head commit but close different
    issues each keep their own answer: one labelled, the other not.
  - Two evaluations of one pull request that finish out of order, such as
    one started by `closed` that is still running when `reopened` arrives,
    leave the label that the issue's current state calls for.
  - A full pass fails a pull request whose only issue was deleted, or
    transferred to another repository.

Because it adds privileged triggers, this phase lands as its own pull
request, and only with the maintainer's explicit approval.

## Current cases

- #444: the claimant asked properly. Assign the issue.
- #445 and #446: the author claimed #445 in a comment, as the issue text
  invited, but neither waited nor linked the issue. Treat this as a gap in
  our text, not the author's fault: assign #445, then ask for `Closes #445`
  and for `uv run ruff format`. CI's format check fails on two files in #446,
  and the unrelated reformatting uses up most of the headroom the issue was
  meant to free. Close the pull request only if the author does not respond.
- #447: it introduced the rule before the rule covered maintainers, so it
  linked no issue at first. It now closes #449. It also carried an aiohttp
  update for #450 until #448 landed one on `main`, and dropped it when it
  was rebased onto `main`.
- #448: Dependabot's aiohttp 3.14.4 update, exempt as a Dependabot pull
  request, merged first. A Dependabot pull request names no issue, so it
  fixed #450 without closing it, and #450 is closed by hand.
