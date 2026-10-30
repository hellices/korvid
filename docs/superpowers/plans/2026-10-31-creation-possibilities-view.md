# Possible Creation Constraints, View (#411 Q2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the constraints a workload's next Pod could run into in the `:blocked` view (#529, the view half of slice Q2 of #411).

**Architecture:** `ui/widgets/creation_constraints_screen.py` gains a fourth section and its notes, both computed from `possible_constraints(report, now)` (#528). The controller, command and wiring are unchanged: the report already carries the template.

**Tech Stack:** Python 3.11+, Textual, Rich `Text`, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-30-creation-constraints-design.md`, section "Possible constraints (P2)".
- **A possibility is never a cause or a forecast,** and the view says so whenever it shows one.
- **"No ResourceQuota or LimitRange observed constrains this template"** only when `Possibilities.nothing_constrains`; otherwise "Not established" with every reason that applies.
- **Claim rows are conditional:** they hold only if the claim is created.
- **Literal text:** every cell and note is plain `Text`.
- **Stable rows:** the timer re-renders cells by row index, so the number of rows must not depend on the clock. `possible_constraints` uses `now` only for `nothing_constrains`, which is a note.
- **Size limits:** `scripts/check_source_size.py` is not edited.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **The policy is named once per run of rows,** as the quota and LimitRange sections name each policy on its first row only. Rows say "quota NAME" or "LimitRange NAME", since one section mixes both kinds.
- **A claim row's finding carries the condition:** "would exceed, if the claim is created", or "if the claims are created" for a row about several claims.
- **The notes say why nothing is not known to constrain the template:** the lists not all read, a RuntimeClass, or a claim without a storage class, mirroring the core's conditions.
- **The version-matrix workflow** now also runs when `creation_demand.py` or `creation_possibilities.py` changes, since its scenario checks them.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/ui/widgets/creation_constraints_screen.py` | modify | the Possible constraints section and its notes |
| `tests/ui/test_creation_constraints_screen.py` | modify | rows, notes and Enter |
| `tests/version_matrix/test_scenarios.py`, `.github/workflows/k8s-version-matrix.yml` | modify | the live check and its path filters |
| `docs/tui.md`, `docs/keybindings.md`, `docs/release-notes/unreleased.md` | modify | what the section shows and how to read it |

---

### Task 1: The section and its notes

**Files:**
- Modify: `src/korvid/ui/widgets/creation_constraints_screen.py`
- Test: `tests/ui/test_creation_constraints_screen.py`

**Interfaces:**
- Consumes: `possible_constraints(report: CreationReport, now: datetime) -> Possibilities | None`, `Possibility(policy_kind, policy, uid, subject, finding, detail, conditional)` from `korvid.core.creation_possibilities`.
- Produces: a fifth entry in `table_rows`' sections, and `_possibility_notes` in `report_notes`.

- [ ] **Step 1: Write the failing tests.** Extend the test file's `creation_workloads` import with `ClaimTemplate`, `ContainerDemand` and `PodTemplate`, then append:

```python
# --- possible constraints (#411 Q2) -----------------------------------------

APP = ContainerDemand("app", "container", (("cpu", "500m"),), (("cpu", "500m"),))
TEMPLATE = PodTemplate((APP,), ())
COMPUTE = QuotaProjection(
    "compute",
    "uid-c",
    (
        QuotaEntry("requests.cpu", QuotaGroup.COMPUTE, "2", "2", Usage.AT_LIMIT),
        QuotaEntry("pods", QuotaGroup.COUNT, "20", "20", Usage.AT_LIMIT),
    ),
)
TIGHT = LimitRangeProjection("tight", "uid-t", (LimitValue("Container", "cpu", max="250m"),))
_POSSIBLE = "Possible constraints — "
_CAVEAT = (
    "A possible constraint is not a cause or a forecast: quota status is not a reservation, "
    "other Pods are created concurrently, and admission webhooks are not observed."
)
_UNCONSTRAINED = "No ResourceQuota or LimitRange observed constrains this template."
_OPEN = "Not established: whether a ResourceQuota or LimitRange constrains this template: "


def _with(template: PodTemplate | None, **changes: Any) -> CreationReport:
    return _report(workload=replace(WORKLOAD, template=template), **changes)


def _possible(report: CreationReport, now: datetime = NOW) -> list[Row]:
    rows = table_rows(report, now)
    start = next(
        i
        for i, row in enumerate(rows)
        if isinstance(row, Heading) and row.text.startswith(_POSSIBLE)
    )
    return [row for row in rows[start + 1 :] if isinstance(row, Row)]


def test_possible_constraints_come_last_naming_each_policy_once_per_run() -> None:
    report = _with(TEMPLATE, failures=(), quotas=(COMPUTE,), limit_ranges=(TIGHT,))

    headings = [row.text for row in table_rows(report, NOW) if isinstance(row, Heading)]
    possible = _possible(report)

    assert headings[-1].startswith(_POSSIBLE + "what the next Pod could run into as now observed")
    quota = Destination("", "resourcequotas", "shop", "compute", "uid-c")
    limits = Destination("", "limitranges", "shop", "tight", "uid-t")
    assert possible == [
        Row(
            ("quota compute", "requests.cpu", "would exceed", "needs 500m; none remaining"),
            quota,
        ),
        Row(("", "pods", "at its limit", "20 of 20 Pods exist"), quota),
        Row(
            ("LimitRange tight", "cpu", "above the maximum", "app: limit 500m is above 250m"),
            limits,
        ),
    ]


def test_a_claim_row_holds_only_if_the_claim_is_created() -> None:
    storage = QuotaProjection(
        "storage",
        "uid-s",
        (QuotaEntry("requests.storage", QuotaGroup.STORAGE, "100Gi", "95Gi", Usage.BELOW),),
    )
    one = replace(TEMPLATE, claims=(ClaimTemplate("data", "10Gi", "gold"),))
    two = replace(
        TEMPLATE,
        claims=(ClaimTemplate("data", "4Gi", "gold"), ClaimTemplate("logs", "4Gi", "gold")),
    )

    single = _possible(_with(one, quotas=(storage,), limit_ranges=()))
    both = _possible(_with(two, quotas=(storage,), limit_ranges=()))

    assert [row.cells for row in single] == [
        (
            "quota storage",
            "claim data: requests.storage",
            "would exceed, if the claim is created",
            "needs 10Gi; 5Gi remaining",
        )
    ]
    assert [row.cells for row in both] == [
        (
            "quota storage",
            "claims data, logs: requests.storage",
            "would exceed, if the claims are created",
            "needs 8Gi; 5Gi remaining",
        )
    ]


def test_possible_constraints_are_never_a_cause_and_say_when_defaults_were_applied() -> None:
    bare = PodTemplate((ContainerDemand("app", "container", (), ()),), ())

    notes = _plain(report_notes(_with(bare, quotas=(COMPUTE,)), NOW))

    assert _CAVEAT in notes
    assert (
        "Container defaults from the LimitRanges fill what the template leaves unset; other "
        "admission plugins can change a Pod further."
    ) in notes


def test_nothing_constrains_the_template_only_when_the_core_concludes_it() -> None:
    report = _with(TEMPLATE, quotas=(), limit_ranges=())

    notes = _plain(report_notes(report, NOW))

    assert _UNCONSTRAINED in notes
    assert _CAVEAT not in notes
    assert not any(
        isinstance(row, Heading) and row.text.startswith(_POSSIBLE)
        for row in table_rows(report, NOW)
    )


@pytest.mark.parametrize(
    ("template", "changes", "reason"),
    [
        (
            replace(TEMPLATE, runtime_class="kata"),
            {},
            "a RuntimeClass may add overhead, and RuntimeClasses are not read",
        ),
        (
            replace(TEMPLATE, claims=(ClaimTemplate("data", "1Gi", None),)),
            {},
            "a claim without a storage class may get the cluster's default, which is not read",
        ),
        (
            TEMPLATE,
            {"quotas_observation": _seen(_source("", "resourcequotas"), Basis.TRUNCATED)},
            "not every ResourceQuota and LimitRange is listed",
        ),
        (
            TEMPLATE,
            {"limit_ranges_observation": _seen(_source("", "limitranges"), Basis.FORBIDDEN)},
            "not every ResourceQuota and LimitRange is listed",
        ),
    ],
    ids=["runtime-class", "default-storage-class", "quotas-not-listed", "limit-ranges-not-listed"],
)
def test_why_nothing_is_not_known_to_constrain_the_template_is_said(
    template: PodTemplate, changes: dict[str, Any], reason: str
) -> None:
    notes = _plain(report_notes(_with(template, quotas=(), limit_ranges=(), **changes), NOW))

    assert _OPEN + reason in notes
    assert _UNCONSTRAINED not in notes


def test_a_list_gone_stale_stops_ruling_constraints_out() -> None:
    report = _with(TEMPLATE, quotas=(), limit_ranges=())

    notes = _plain(report_notes(report, NOW + timedelta(minutes=5)))

    assert _OPEN + "not every ResourceQuota and LimitRange is listed" in notes
    assert _UNCONSTRAINED not in notes


def test_an_unreadable_template_leaves_the_possibilities_open() -> None:
    unread = _plain(report_notes(_with(None), NOW))
    gone = _plain(
        report_notes(
            _report(
                workload=None, workload_observation=_seen(WORKLOAD_SOURCE, Basis.OBJECT_ABSENT)
            ),
            NOW,
        )
    )

    assert (
        "Not established: what could constrain the next Pod: its template could not be read"
        in unread
    )
    assert not any("constrain" in note for note in gone)


async def test_enter_on_a_possible_constraint_opens_its_policy() -> None:
    report = _with(TEMPLATE, failures=(), quotas=(), limit_ranges=(TIGHT,))
    app = _Host(CreationConstraintsScreen(report, lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        table = app.screen.query_one(DataTable)
        # From the LimitRange's own row, past the next heading, to the possibility.
        await pilot.press("down", "down")
        assert table.get_row_at(table.cursor_row)[0].plain == "LimitRange tight"
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [Destination("", "limitranges", "shop", "tight", "uid-t")]
```

- [ ] **Step 2: Run them to see them fail.**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_creation_constraints_screen.py -q`
Expected: the new tests fail; there is no Possible constraints heading or note yet.

- [ ] **Step 3: Implement the rows.** Import `Possibility` and `possible_constraints`, add the heading and the policy maps after `_LIMIT_RANGES`:

```python
_POSSIBLE = "Possible constraints — what the next Pod could run into as now observed"
_POLICY_RESOURCES = {"ResourceQuota": "resourcequotas", "LimitRange": "limitranges"}
_POLICY_LABELS = {"ResourceQuota": "quota", "LimitRange": "LimitRange"}
```

Add before `table_rows`:

```python
def _possibility_rows(report: CreationReport, now: datetime) -> list[Row]:
    """Each possible constraint, its policy named on the first of its consecutive rows."""
    possibilities = possible_constraints(report, now)
    rows: list[Row] = []
    previous: tuple[str, str] | None = None
    for possibility in possibilities.rows if possibilities is not None else ():
        policy = (possibility.policy_kind, possibility.policy)
        label = "" if policy == previous else f"{_POLICY_LABELS[policy[0]]} {policy[1]}"
        previous = policy
        destination = Destination(
            "",
            _POLICY_RESOURCES[possibility.policy_kind],
            report.target.namespace,
            possibility.policy,
            possibility.uid,
        )
        cells = (label, possibility.subject, _finding_text(possibility), possibility.detail)
        rows.append(Row(cells, destination))
    return rows


def _finding_text(possibility: Possibility) -> str:
    if not possibility.conditional:
        return possibility.finding.value
    created = "the claims are" if possibility.subject.startswith("claims ") else "the claim is"
    return f"{possibility.finding.value}, if {created} created"
```

and append `(_POSSIBLE, _possibility_rows(report, now))` to `table_rows`' sections.

- [ ] **Step 4: Implement the notes.** Add before `report_notes`, and append `*_possibility_notes(report, now)` to its list:

```python
def _unconstrained_reasons(report: CreationReport, now: datetime) -> list[str]:
    """Why the core could not conclude that nothing constrains the template."""
    template = report.workload.template if report.workload is not None else None
    reasons: list[str] = []
    if not (report.quotas_conclusive(now) and report.limit_ranges_conclusive(now)):
        reasons.append("not every ResourceQuota and LimitRange is listed")
    if template is not None and template.runtime_class is not None:
        reasons.append("a RuntimeClass may add overhead, and RuntimeClasses are not read")
    if template is not None and any(claim.storage_class is None for claim in template.claims):
        reasons.append(
            "a claim without a storage class may get the cluster's default, which is not read"
        )
    return reasons


def _possibility_notes(report: CreationReport, now: datetime) -> list[Text]:
    if report.workload is None:
        return []
    possibilities = possible_constraints(report, now)
    if possibilities is None:
        return [
            not_established("what could constrain the next Pod: its template could not be read")
        ]
    notes: list[Text] = []
    if possibilities.defaults_applied:
        notes.append(
            Text(
                "Container defaults from the LimitRanges fill what the template leaves unset; "
                "other admission plugins can change a Pod further."
            )
        )
    if possibilities.rows:
        notes.append(
            Text(
                "A possible constraint is not a cause or a forecast: quota status is not a "
                "reservation, other Pods are created concurrently, and admission webhooks are "
                "not observed."
            )
        )
    elif possibilities.nothing_constrains:
        notes.append(Text("No ResourceQuota or LimitRange observed constrains this template."))
    else:
        reasons = "; ".join(_unconstrained_reasons(report, now))
        notes.append(
            not_established(
                f"whether a ResourceQuota or LimitRange constrains this template: {reasons}"
            )
        )
    return notes


def report_notes(report: CreationReport, now: datetime) -> list[Text]:
    """What the evidence says about the workload, and what it does not establish."""
    return [
        *_workload_notes(report, now),
        *_failure_notes(report),
        *_completeness_notes(report, now),
        *_possibility_notes(report, now),
    ]
```

- [ ] **Step 5: Run the tests to see them pass.**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_creation_constraints_*.py -q`
Expected: all pass.

- [ ] **Step 6: Commit.**

### Task 2: The version matrix

**Files:**
- Modify: `tests/version_matrix/test_scenarios.py`, `.github/workflows/k8s-version-matrix.yml`

- [ ] **Step 1:** Import `Finding` and `possible_constraints` from `korvid.core.creation_possibilities`, and end `test_a_quota_rejection_is_read_next_to_the_namespace_policies` with:

```python
    # The template sets no resources, so the LimitRange's CPU defaults fill them,
    # within its maximum: only the Pod count is left to constrain the next Pod.
    possibilities = possible_constraints(report, datetime.now(UTC))
    assert possibilities is not None
    assert possibilities.defaults_applied
    assert [(p.policy, p.subject, p.finding, p.detail) for p in possibilities.rows] == [
        ("matrix-pods", "pods", Finding.AT_LIMIT, "0 of 0 Pods exist")
    ]
```

- [ ] **Step 2:** Add `src/korvid/core/creation_demand.py` and `src/korvid/core/creation_possibilities.py` to both path filters of `.github/workflows/k8s-version-matrix.yml`.

- [ ] **Step 3: Commit.** The scenario runs only on kind clusters in CI.

### Task 3: Document it

- [ ] `docs/tui.md`: a Possible constraints row in the section table, and a "never a cause or a forecast" reading note.
- [ ] `docs/keybindings.md`: `:blocked` also shows what the next Pod could run into.
- [ ] `docs/release-notes/unreleased.md`: the section, in the Workload creation failures entry.
- [ ] Commit.

### Task 4: Run the full gate

Run: `make check`
Expected: every step passes.
