"""No document positions korvid against another tool.

korvid describes what it does; it does not rank itself against other
Kubernetes or assistant tools. A comparison table once reached PyPI as the
project description, so this guard scans every tracked text file for the
names of competing products.

MCP host names (VS Code, Claude Code, Cursor, Zed, GitHub Copilot) are not
on the list: they tell users how to connect, and compare nothing.
"""

from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_SELF = Path(__file__).relative_to(_ROOT).as_posix()

#: Bounded by letters and digits only, so neither a hyphen ("non-K9s") nor
#: Markdown's underscore emphasis ("_K9s_") hides a name.
_COMPETING_PRODUCTS = re.compile(
    r"(?<![A-Za-z0-9])(?:k9s|ks-ai|kubectl-ai|k8sgpt|holmesgpt|headlamp|freelens|openlens|aider"
    r"|continue\.dev|continuedev|cline|openclaw)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _tracked_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    names = tuple(
        sorted(name for name in result.stdout.decode("utf-8").split("\0") if name and name != _SELF)
    )
    assert "README.md" in names, "git listed no README, so this scan would clear anything"
    return names


def _document_text(data: bytes) -> str | None:
    """The file as text, whatever its suffix, or `None` for a binary file."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _mentions(text: str) -> list[str]:
    """Names in `text` as rendered, so a character reference (`K&#57;s`) counts."""
    return [match.group(0) for match in _COMPETING_PRODUCTS.finditer(html.unescape(text))]


def test_no_tracked_document_names_a_competing_product() -> None:
    found: list[str] = []
    scanned = 0
    for name in _tracked_files():
        path = _ROOT / name
        if not path.is_file():
            continue
        text = _document_text(path.read_bytes())
        if text is None:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            found.extend(f"{name}:{number}: {hit}" for hit in _mentions(line))
    assert scanned > 100, f"only {scanned} tracked files read as text"
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
        "A non-K9s workflow.",
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


@pytest.mark.parametrize(
    "line",
    [
        "Unlike _K9s_, korvid shares your screen.",
        "Unlike __K9s__, korvid shares your screen.",
        "Unlike *K9s*, korvid shares your screen.",
        "Unlike K&#57;s, korvid shares your screen.",
        "Unlike K&#x39;s, korvid shares your screen.",
        "Unlike &#75;9s, korvid shares your screen.",
    ],
)
def test_the_guard_sees_through_emphasis_and_character_references(line: str) -> None:
    assert _mentions(line)


@pytest.mark.parametrize(
    "data",
    [b"Unlike K9s, korvid ...\n", "Comparación con K9s\n".encode()],
    ids=["ascii", "utf-8"],
)
def test_any_utf8_file_is_read_as_a_document_whatever_its_suffix(data: bytes) -> None:
    text = _document_text(data)
    assert text is not None
    assert _mentions(text)


@pytest.mark.parametrize(
    "data",
    [b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", b"\xff\xd8\xff\xe0 not utf-8 \xfe"],
    ids=["nul-byte", "not-utf-8"],
)
def test_a_binary_file_is_not_read_as_a_document(data: bytes) -> None:
    assert _document_text(data) is None
