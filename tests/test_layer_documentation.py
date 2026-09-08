"""AGENTS.md's layer table is the readable copy of `tach.toml`.

`tach` is what actually rejects an illegal import; the table is what a
contributor reads before writing one. When the two disagree the table is
worse than nothing — it describes a boundary the checker does not enforce,
or hides one it does. That is how `korvid.option_keys` came to be
undocumented: `core` and `providers` may not import each other, so the
credential-key vocabulary they must agree on lives in a leaf module below
both, `tach.toml` grants both the dependency, and the table said `core/`
imports `k8s` and `providers/` imports `agent` and nothing else.

This test compares the two directly, so the table cannot drift again.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
_PACKAGE = "korvid."

#: `| \`name/\` | a, b, c | ... |` — the layer rows, and only those.
_ROW = re.compile(r"^\|\s*`(?P<layer>[\w.]+)/?`\s*\|(?P<imports>[^|]*)\|")


def _tach_rules() -> dict[str, frozenset[str]]:
    data = tomllib.loads((ROOT / "tach.toml").read_text(encoding="utf-8"))
    return {
        module["path"].removeprefix(_PACKAGE): frozenset(
            dependency.removeprefix(_PACKAGE) for dependency in module.get("depends_on", ())
        )
        for module in data["modules"]
    }


def _documented_rules(known: frozenset[str]) -> dict[str, frozenset[str]]:
    """The table's rows, read the way a contributor reads them.

    Only names `tach` knows are collected out of the "May import" cell, so
    a prose answer — `k8s/`'s "(stdlib + kubernetes client)" — reads as the
    empty set it is rather than as a parse failure.
    """
    rules: dict[str, frozenset[str]] = {}
    for line in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        cell = match["imports"]
        rules[match["layer"]] = frozenset(
            name for name in re.findall(r"[\w.]+", cell) if name in known
        )
    return rules


def test_the_layer_table_documents_exactly_the_modules_tach_enforces() -> None:
    tach = _tach_rules()
    documented = _documented_rules(frozenset(tach))

    assert set(documented) == set(tach)


def test_every_documented_layer_lists_the_dependencies_tach_grants_it() -> None:
    tach = _tach_rules()
    documented = _documented_rules(frozenset(tach))

    assert documented == tach


def test_the_shared_option_key_vocabulary_is_documented_as_a_stdlib_leaf() -> None:
    """The one rule `core` and `providers` share, and why it can be shared.

    Named explicitly because it is the row whose absence started this: a
    reader who does not know the module exists will copy the vocabulary
    into whichever gate they are editing, which is the drift it was
    extracted to end.
    """
    tach = _tach_rules()
    assert tach["option_keys"] == frozenset()
    assert "option_keys" in tach["core"]
    assert "option_keys" in tach["providers"]

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "option_keys.py" in agents
    assert "korvid.option_keys" in agents
