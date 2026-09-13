"""Auto-discovered, fixed-clock Pulse scenarios without a live cluster."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from korvid.core.pulse import PulseCoverage, PulseModel, PulseSnapshot
from korvid.core.pulse_rules import DeploymentPulseRule, PodPulseRule

_SCENARIOS = Path(__file__).parents[1] / "fixtures" / "pulse"


def _timestamp(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _assert_items(snapshot: PulseSnapshot, expected: dict[str, Any]) -> None:
    observed: dict[str, set[str | None]] = {
        "current_reasons": {item.reason for item in snapshot.current},
        "current_rule_ids": {item.finding.rule_id for item in snapshot.current if item.finding},
        "current_names": {item.target.name for item in snapshot.current},
        "recent_reasons": {item.reason for item in snapshot.recent},
        "recent_namespaces": {item.target.namespace for item in snapshot.recent},
        "recent_uids": {item.target.uid for item in snapshot.recent},
    }
    for field, values in observed.items():
        assert set(expected.get(field, ())) <= values, (field, values)
        assert values.isdisjoint(expected.get(f"forbidden_{field}", ())), (field, values)
    for field, count in (
        ("current_count", len(snapshot.current)),
        ("recent_count", len(snapshot.recent)),
        ("dropped", snapshot.dropped),
        ("current_dropped", snapshot.current_dropped),
    ):
        if field in expected:
            assert count == expected[field], (field, count)
    for forbidden in expected.get("forbidden_text", ()):
        assert forbidden not in repr(snapshot)


def _assert_coverage(snapshot: PulseSnapshot, expected: dict[str, Any]) -> None:
    observed = {coverage.source: coverage.state for coverage in snapshot.coverage}
    for source, state in expected.get("coverage", {}).items():
        assert observed.get(source) == state, (source, observed)


def _apply_sources(model: PulseModel, step: dict[str, Any], now: datetime) -> None:
    for source, observation in step.get("sources", {}).items():
        coverage = PulseCoverage(
            source,
            observation.get("state", "complete"),
            _timestamp(observation.get("observed_at", now)),
            observation.get("detail", ""),
        )
        model.replace_source(source, observation.get("objects", ()), coverage)


def test_pulse_scenarios_exist() -> None:
    assert any(_SCENARIOS.glob("*.yaml")), "Pulse scenarios must be discovered from fixture files"


@pytest.mark.parametrize(
    ("field", "expected"),
    [("current_dropped", 1), ("recent_namespaces", ["default"]), ("recent_uids", ["exact-uid"])],
)
def test_scenario_assertions_cover_current_loss_and_event_identity(
    field: str, expected: Any
) -> None:
    with pytest.raises(AssertionError, match=field):
        _assert_items(PulseSnapshot(0, None, (), (), ()), {field: expected})


@pytest.mark.parametrize(
    "scenario_path", sorted(_SCENARIOS.glob("*.yaml")), ids=lambda path: path.stem
)
def test_pulse_scenario(scenario_path: Path) -> None:
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    assert isinstance(scenario, dict)
    start = _timestamp(scenario["now"])
    assert start.utcoffset() is not None
    limits = scenario.get("limits", {})
    model = PulseModel(
        (PodPulseRule(), DeploymentPulseRule()),
        max_events=limits.get("max_events", 100),
        max_bytes=limits.get("max_bytes", 131072),
        retention=timedelta(seconds=limits.get("retention_seconds", 900)),
        max_current_findings=limits.get("max_current_findings", 200),
        max_current_bytes=limits.get("max_current_bytes", 262144),
    )
    epoch = scenario.get("epoch", 1)
    model.reset(epoch, scenario.get("scope"))
    assert scenario["steps"]
    for step in scenario["steps"]:
        now = start + timedelta(seconds=step.get("after_seconds", 0))
        _apply_sources(model, step, now)
        for event in step.get("warnings", ()):
            model.record_warning(event, epoch, now)
        snapshot = model.snapshot(now)
        assert all(item.finding is not None for item in snapshot.current)
        assert step["expect"]
        _assert_items(snapshot, step["expect"])
        _assert_coverage(snapshot, step["expect"])
