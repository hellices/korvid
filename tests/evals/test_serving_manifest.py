"""The eval serving manifest must stay pinned (#235).

The 2026-08-10 scoreboard rows are unreproducible because the deployment
served a floating tag and nothing recorded what answered. A test is cheaper
than discovering the same gap after the next campaign's node is gone.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_MANIFEST = Path(__file__).parents[2] / "deploy" / "eval" / "ollama.yaml"


def _documents() -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_MANIFEST.read_text(encoding="utf-8")) if doc]


def _containers() -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    for doc in _documents():
        if doc.get("kind") == "Deployment":
            containers.extend(doc["spec"]["template"]["spec"]["containers"])
    return containers


def test_the_manifest_parses_and_declares_the_serving_deployment() -> None:
    kinds = {doc.get("kind") for doc in _documents()}
    assert kinds == {"Deployment", "PersistentVolumeClaim"}


@pytest.mark.parametrize("container", _containers())
def test_every_image_names_an_explicit_release_tag(container: dict[str, Any]) -> None:
    """`:latest` — or no tag at all — makes the measured version a coincidence."""
    image = container["image"]
    _, _, tag = image.partition(":")
    assert tag, f"{image} has no tag"
    assert tag != "latest", f"{image} must name a release, not a floating tag"
    assert re.fullmatch(r"v?\d+\.\d+\.\d+", tag), f"{image} must name a released version"


@pytest.mark.parametrize("container", _containers())
def test_a_pinned_tag_is_not_re_resolved_on_every_pull(container: dict[str, Any]) -> None:
    """`imagePullPolicy: Always` would let a retagged release slip in."""
    assert container.get("imagePullPolicy") != "Always"
