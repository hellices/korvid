"""Capture of the serving environment a scoreboard row was measured on (#235).

A row is only reproducible if the thing doing the inference is recorded.
The 2026-08-10 matrix was served by a floating `ollama/ollama:latest` tag
and nothing captured which version answered, so those rows cannot be
re-served under the same conditions and the gap was only discovered in
review.

This module is deliberately pure: it turns already-fetched probe payloads
into a metadata block. The HTTP calls live in the CLI, so the mapping —
which is where the arch-prefixed keys and the missing-field bookkeeping
live — is testable without a server.

Every field is optional. A probe that fails records the gap in
`unavailable` rather than omitting the key, because an omitted key makes an
unpinned run look identical to a pinned one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ProbeResult", "ollama_root", "serving_metadata"]


@dataclass(frozen=True)
class ProbeResult:
    """Raw payloads from the serving endpoint, each independently optional.

    Attributes:
        version: `GET /api/version`.
        show: `POST /api/show` for the model under test.
        tags: `GET /api/tags`, which is where the digest lives — `/api/show`
            does not return one.
        error: why the probe could not complete, when it could not.
    """

    version: dict[str, Any] | None = None
    show: dict[str, Any] | None = None
    tags: dict[str, Any] | None = None
    error: str | None = None


def ollama_root(base_url: str) -> str:
    """Strip the OpenAI-compatibility suffix to reach the native API.

    korvid talks to `/v1`; the version, show and tags endpoints sit at the
    server root.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root.rstrip("/")


def serving_metadata(
    *,
    model: str,
    probe: ProbeResult,
    warmup: bool = False,
) -> dict[str, Any]:
    """Build the `serving` block recorded in a run's JSON artifact.

    Args:
        model: the model name the run was configured with.
        probe: whatever the endpoint answered.
        warmup: whether a throwaway request preceded the first scored
            scenario. Defaults to `False` because the harness performs none
            unless asked.

    Returns:
        The metadata block. `unavailable` lists the pinning fields that
        could not be captured, in a fixed order so artifacts diff cleanly.
    """
    version = _version(probe.version)
    quantization = _quantization(probe.show)
    context_length = _context_length(probe.show)
    digest = _digest(probe.tags, model)
    meta: dict[str, Any] = {
        "model": model,
        "engine": {"name": "ollama" if version else None, "version": version},
        "digest": digest,
        "quantization": quantization,
        "context_length": context_length,
        "parameter_size": _parameter_size(probe.show),
        "warmup": warmup,
        "unavailable": _unavailable(version, digest, quantization, context_length),
    }
    if probe.error is not None:
        meta["error"] = probe.error
    return meta


def _unavailable(
    version: str | None,
    digest: str | None,
    quantization: str | None,
    context_length: int | None,
) -> list[str]:
    captured = {
        "engine": version,
        "digest": digest,
        "quantization": quantization,
        "context_length": context_length,
    }
    return [name for name, value in captured.items() if value is None]


def _version(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    value = payload.get("version")
    return value if isinstance(value, str) and value else None


def _quantization(payload: dict[str, Any] | None) -> str | None:
    details = _details(payload)
    value = details.get("quantization_level")
    return value if isinstance(value, str) and value else None


def _parameter_size(payload: dict[str, Any] | None) -> str | None:
    details = _details(payload)
    value = details.get("parameter_size")
    return value if isinstance(value, str) and value else None


def _details(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    details = payload.get("details")
    return details if isinstance(details, dict) else {}


def _context_length(payload: dict[str, Any] | None) -> int | None:
    """`model_info` names the key after the architecture, e.g.
    `qwen3.context_length`. Prefer the declared architecture, but fall back
    to any `*.context_length` so a mismatch does not lose the value."""
    if not payload:
        return None
    info = payload.get("model_info")
    if not isinstance(info, dict):
        return None
    architecture = info.get("general.architecture")
    if isinstance(architecture, str):
        declared = info.get(f"{architecture}.context_length")
        if isinstance(declared, int):
            return declared
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


def _digest(payload: dict[str, Any] | None, model: str) -> str | None:
    if not payload:
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, dict) or entry.get("name") != model:
            continue
        digest = entry.get("digest")
        return digest if isinstance(digest, str) and digest else None
    return None
