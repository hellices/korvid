"""Conversation journey CLI tests."""

from __future__ import annotations

from korvid.evals.journeys_cli import _parse_args


def test_journey_cli_defaults_to_bundled_pack_and_three_reps() -> None:
    args = _parse_args([])
    assert args.reps == 3
    assert args.profile == "small"
    assert args.live is False


def test_journey_cli_accepts_live_mode_and_outputs() -> None:
    args = _parse_args(
        [
            "--live",
            "--reps",
            "1",
            "--out",
            "report.md",
            "--json",
            "report.json",
        ]
    )
    assert args.live is True
    assert args.out.name == "report.md"
    assert args.json.name == "report.json"
