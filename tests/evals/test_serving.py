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
_TAGS = {
    "models": [
        {"name": "qwen3:4b", "digest": "aaa"},
        {"name": "qwen3:8b", "digest": "bbb"},
    ]
}


def test_serving_metadata_records_every_pinning_field() -> None:
    meta = serving_metadata(
        model="qwen3:8b",
        probe=ProbeResult(version=_VERSION, show=_SHOW, tags=_TAGS),
        warmup=True,
    )
    assert meta == {
        "model": "qwen3:8b",
        "engine": {"name": "ollama", "version": "0.5.1"},
        "digest": "bbb",
        "quantization": "Q4_K_M",
        "context_length": 40960,
        "parameter_size": "8.0B",
        "warmup": True,
        "unavailable": [],
    }


def test_context_length_is_read_from_the_architecture_prefixed_key() -> None:
    """`model_info` names the field after the architecture, not generically."""
    show = {
        "details": {"quantization_level": "Q8_0"},
        "model_info": {"general.architecture": "llama", "llama.context_length": 8192},
    }
    meta = serving_metadata(model="m", probe=ProbeResult(version=_VERSION, show=show, tags=_TAGS))
    assert meta["context_length"] == 8192


def test_context_length_falls_back_to_any_context_length_key() -> None:
    """A mismatched `general.architecture` must not lose the value."""
    show = {
        "details": {},
        "model_info": {"general.architecture": "wrong", "gemma3.context_length": 131072},
    }
    meta = serving_metadata(model="m", probe=ProbeResult(version=_VERSION, show=show, tags=_TAGS))
    assert meta["context_length"] == 131072


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


def test_partial_probes_report_only_the_missing_fields() -> None:
    meta = serving_metadata(model="qwen3:8b", probe=ProbeResult(version=_VERSION, tags=_TAGS))
    assert meta["engine"]["version"] == "0.5.1"
    assert meta["digest"] == "bbb"
    assert meta["unavailable"] == ["quantization", "context_length"]


def test_an_unknown_model_leaves_the_digest_unpinned() -> None:
    meta = serving_metadata(
        model="not-pulled", probe=ProbeResult(version=_VERSION, show=_SHOW, tags=_TAGS)
    )
    assert meta["digest"] is None
    assert "digest" in meta["unavailable"]


def test_warmup_defaults_to_false_because_the_harness_performs_none() -> None:
    meta = serving_metadata(model="m", probe=ProbeResult())
    assert meta["warmup"] is False


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://host:11434/v1", "http://host:11434"),
        ("http://host:11434/v1/", "http://host:11434"),
        ("http://host:11434", "http://host:11434"),
        ("http://host:11434/", "http://host:11434"),
        ("https://host/openai/v1", "https://host/openai"),
    ],
)
def test_ollama_root_strips_the_openai_compatibility_suffix(base_url: str, expected: str) -> None:
    assert ollama_root(base_url) == expected
