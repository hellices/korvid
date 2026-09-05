"""Serving-environment capture (#235).

A published scoreboard row is only reproducible if the thing doing the
inference is recorded. These tests pin the shape of that record and, more
importantly, pin that a probe failure degrades to a documented gap instead
of taking the run down with it.
"""

from __future__ import annotations

import pytest

from korvid.evals.serving import ProbeResult, ollama_root, serving_metadata

_VERSION = {"version": "0.5.1"}
_SHOW = {
    "details": {"family": "qwen3", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
}
_PS = {"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "context_length": 4096}]}
_TAGS = {
    "models": [
        {"name": "qwen3:4b", "digest": "aaa"},
        {"name": "qwen3:8b", "digest": "bbb"},
    ]
}


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://host:11434/v1", "http://host:11434"),
        ("http://host:11434", "http://host:11434"),
    ],
)
def test_ollama_root_reaches_the_native_api(base_url: str, expected: str) -> None:
    assert ollama_root(base_url) == expected


def test_serving_metadata_records_every_pinning_field() -> None:
    meta = serving_metadata(
        model="qwen3:8b",
        probe=ProbeResult(version=_VERSION, show=_SHOW, tags=_TAGS, ps=_PS),
        warmup=True,
    )
    assert meta == {
        "model": "qwen3:8b",
        "engine": {"name": "ollama", "version": "0.5.1"},
        "digest": "bbb",
        "quantization": "Q4_K_M",
        "context_length": 4096,
        "max_context_length": 40960,
        "parameter_size": "8.0B",
        "warmup": True,
        "unavailable": [],
    }


def test_a_failed_probe_is_recorded_as_a_gap_rather_than_raising() -> None:
    """The whole point of the issue: an unpinned field must be visible.

    Silently omitting the key would make an unpinned run indistinguishable
    from a pinned one, which is the failure this capture exists to prevent.
    """
    meta = serving_metadata(model="qwen3:8b", probe=ProbeResult(error="connect timeout"))
    assert meta["engine"] == {"name": None, "version": None}
    assert meta["digest"] is None
    assert meta["quantization"] is None
    assert meta["context_length"] is None
    assert meta["unavailable"] == ["engine", "digest", "quantization", "context_length"]
    assert meta["error"] == "connect timeout"
