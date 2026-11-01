"""No document positions korvid against another tool.

korvid describes what it does; it does not rank itself against other
Kubernetes or assistant tools. A comparison table once reached PyPI as the
project description, so this guard scans every tracked text file for the
names of competing products.

MCP host names (VS Code, Claude Code, Cursor, Zed, GitHub Copilot) are not
on the list: they tell users how to connect, and compare nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_SELF = Path(__file__).relative_to(_ROOT).as_posix()

_TEXT_SUFFIXES = frozenset(
    {".md", ".html", ".yml", ".yaml", ".toml", ".txt", ".py", ".cfg", ".ini", ".json", ".css"}
)

_COMPETING_PRODUCTS = re.compile(
    r"(?<![\w-])(?:k9s|ks-ai|kubectl-ai|k8sgpt|holmesgpt|headlamp|freelens|openlens|aider"
    r"|continue\.dev|continuedev|cline|openclaw)(?!\w)",
    re.IGNORECASE,
)


def _tracked_text_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    names = tuple(
        sorted(
            name
            for name in result.stdout.decode("utf-8").split("\0")
            if Path(name).suffix in _TEXT_SUFFIXES and name != _SELF
        )
    )
    assert "README.md" in names, "git listed no README, so this scan would clear anything"
    return names


def _mentions(text: str) -> list[str]:
    return [match.group(0) for match in _COMPETING_PRODUCTS.finditer(text)]


def test_no_tracked_document_names_a_competing_product() -> None:
    found: list[str] = []
    for name in _tracked_text_files():
        path = _ROOT / name
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            found.extend(f"{name}:{number}: {hit}" for hit in _mentions(line))
    assert not found, "documents must not compare korvid with other tools:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "line",
    [
        "If you know k9s, the keys will feel familiar.",
        "| [kubectl-ai](https://github.com/GoogleCloudPlatform/kubectl-ai) |",
        "Compare K8sGPT's analyzers.",
        "HolmesGPT reaches more data sources.",
        "Aider exposes no override.",
        "Publish a K9s-inspired landing page.",
        "The only attempt, ks-ai, is an early PoC.",
        "Continue.dev's `autodetect.ts` carries 20+.",
        "See https://github.com/continuedev/continue.",
        "Cline allows append-only.",
        "Act like OpenClaw agents.",
    ],
)
def test_the_guard_catches_a_competing_product(line: str) -> None:
    assert _mentions(line)


@pytest.mark.parametrize(
    "line",
    [
        "Connect VS Code, Claude Code, Cursor, or Zed over MCP.",
        "GitHub Copilot Chat can call korvid's tools.",
        "Run `kubectl get pods` on the throwaway cluster.",
        "A k8s cluster at 1.33 or later.",
        "Continue with the next step; the request may decline.",
    ],
)
def test_the_guard_leaves_integration_names_alone(line: str) -> None:
    assert not _mentions(line)
