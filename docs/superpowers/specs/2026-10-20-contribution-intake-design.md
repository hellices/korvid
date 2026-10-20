# Contribution intake: an issue first, an assignment before a pull request

Date: 2026-10-20. Status: phase 1 is in #447; phase 2 is proposed and needs
the maintainer's approval.

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

Promotion will raise the volume. GitHub reports that monthly pull request
volume grew about 3.6 times since 2023, much of it drive-by and AI-assisted
([pull request limits](https://github.blog/open-source/maintainers/how-pull-request-limits-are-cutting-down-the-noise/)).
Without a written intake rule, every unsolicited pull request becomes a
negotiation.

## Goals and non-goals

Goals:

- Every outside pull request maps to an accepted issue assigned to its author.
- Two people do not build the same thing, and review time goes to agreed work.
- Contributors can read the rule before they start, and a decline points to it.

Non-goals:

- No merge automation of any kind (AGENTS.md).
- No change to how maintainers with write access open their own pull requests.
- No closing the door on outside contributions.

## Options considered

| Option | What it does | Verdict |
| --- | --- | --- |
| A. Written rule only | CONTRIBUTING.md, the forms, and the pull request template state the rule; maintainers enforce it by hand | Necessary but not enough as volume grows |
| B. Written rule, GitHub limits, and an intake check | A, plus the open pull request limit for users without write access, plus an automated check that labels pull requests without an assigned issue | **Chosen**, in two phases |
| C. Pull requests from collaborators only | The [February 2026 setting](https://github.blog/changelog/2026-02-13-new-repository-settings-for-configuring-pull-request-access/) lets only users with write access open pull requests | Rejected as the default: every contributor would need write access to a repository that holds release and Homebrew automation. Kept as an emergency lever |

GitHub has no built-in rule that a pull request must close an assigned issue.
The repository has to state it and, at scale, check it.

## Phase 1: the written rule (this pull request)

- CONTRIBUTING.md gains "From issue to pull request":
  1. find or open an issue; form issues are labelled `status: needs-triage`
     until a maintainer accepts or closes them;
  2. ask for it with a short plan;
  3. wait to be assigned, one issue at a time until a first pull request is
     merged; an assignment with no pull request or update for 14 days may be
     released;
  4. open the pull request with `Closes #N` at the top.

  A pull request from someone without write access that does not close an
  issue assigned to its author is closed without review. It can be reopened
  once the issue is assigned. Security fixes go through SECURITY.md.
- Both issue forms add `status: needs-triage`. The feature form says to wait
  for acceptance and assignment.
- The pull request template starts with `Closes #` and has a checklist item
  for it.
- The `status: needs-triage` label exists. The existing `status: tbd` and
  `status: backlog` mean deferred work, not untriaged work.
- The open good first issues (#442–#445) tell claimants to wait for
  assignment.
- `tests/test_contribution_intake.py` keeps the forms, the template, and
  CONTRIBUTING.md stating the same rule.

Repository settings for the maintainer (Settings > General > Pull requests):

- Limit open pull requests for users without write access to 1, with a bypass
  list for trusted contributors
  ([June 2026 setting](https://github.blog/changelog/2026-06-17-limit-open-pull-requests-for-users-without-write-access/)).
  Draft pull requests do not count.
- Keep requiring approval before Actions run for first-time contributors.

## Maintainer routine

- Triage `status: needs-triage` weekly. Accept an issue by labelling it
  `help wanted`, `good first issue`, or scheduling it, then remove the triage
  label. Otherwise close it with the reason.
- Assign on request. Before the assignee's first merge, give them one issue at
  a time. Release an assignment after 14 days without a pull request or update.
- Close an outside pull request that does not close an issue assigned to its
  author, with this reply:

  > Thanks for the pull request. korvid reviews outside pull requests only for
  > issues a maintainer has assigned to their author; see "From issue to pull
  > request" in CONTRIBUTING.md. Please ask for the issue there. Once it is
  > assigned to you, reopen this pull request and add `Closes #N` at the top.

## Phase 2: an intake check (proposed)

A workflow that checks each outside pull request against the rule, so
enforcement does not depend on a maintainer noticing:

- It is triggered by `pull_request_target` (opened, edited, reopened), so it
  runs for first-time contributors without approval. It never checks out or
  runs pull request code, uses no third-party actions, and passes the
  untrusted title and body only through environment variables, never through
  shell interpolation.
- Permissions are `pull-requests: write` and `issues: read`, nothing else.
- It passes immediately only when the author can write to the repository. It
  reads the author's permission level on the repository and lets `admin`,
  `maintain`, or `write` through. `authorAssociation` is not enough, because
  `MEMBER` can be an organization member without write access and
  `COLLABORATOR` can hold only read or triage access, and the rule turns on
  write access.
- Otherwise it reads the pull request's `closingIssuesReferences` through
  GraphQL. It passes only if one linked issue meets every condition:
  - It belongs to this repository: its `repository.id` equals the pull
    request's `baseRepository.id`. A closing reference such as
    `Closes other/repo#N` can name an issue in a repository the author
    controls, where they can open, accept, and assign issues themselves.
    Comparing node IDs rather than `nameWithOwner` strings leaves no room
    for letter case or a rename to confuse the comparison.
  - It is open.
  - It is assigned to the author.
  - It is accepted, meaning it no longer carries `status: needs-triage`. An
    issue that was assigned by mistake while still in triage does not let a
    pull request through.
- On failure it adds `status: needs-issue` and posts the reply above once.
  Closing stays a maintainer's decision until the check has run without false
  positives for a while.
- It ships with tests in the existing workflow-contract style: trigger set,
  permissions, no checkout, no interpolation of pull request fields. The
  decision itself is a pure function of the author's permission and the
  linked issues, tested case by case:
  - `admin`, `maintain`, or `write` passes with no linked issue.
  - `triage`, `read`, or no access with no linked issue fails.
  - An open, accepted issue in this repository assigned to the author passes.
  - The same issue in another repository (`Closes other/repo#N`) fails,
    however it is labelled and assigned there.
  - A closed issue fails, as does an issue assigned to someone else or to
    no one, and an issue still labelled `status: needs-triage`.
  - One passing issue among failing ones passes.

Because it adds a privileged trigger, this phase lands as its own pull
request, and only with the maintainer's explicit approval.

## Current cases

- #444: the claimant asked properly. Assign the issue.
- #445 and #446: the author claimed #445 in a comment, as the issue text
  invited, but neither waited nor linked the issue. Treat this as a gap in
  our text, not the author's fault: assign #445, then ask for `Closes #445`
  and for `uv run ruff format`. CI's format check fails on two files in #446,
  and the unrelated reformatting uses up most of the headroom the issue was
  meant to free. Close the pull request only if the author does not respond.
