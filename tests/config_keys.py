"""Matching a config key *as a key*, in one place.

Two guards need the same question answered — "does this text name this
key, or does it merely contain it?" — and both got it wrong in the same
way before this module existed. A plain `in` test reports a supported
key as if it were the retired singular spelling it replaced, so the
guard fails on correct code and gets widened until it means nothing.
`tests/test_agent_replacement_guard.py` carries the concrete example.

A dotted key is also a *prefix* of every key below it, so the boundary
has to exclude `.` as well as word characters on both sides: `a.b` must
not match `a.bs`, `a.b.c` or `xa.b`. `\\b` alone gets the middle case
wrong, which is why this is a shared helper rather than a regex written
twice that agrees with itself only until someone edits one of them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: A key is bounded by anything that is not a word character or a dot.
_LEFT = r"(?<![\w.])"
_RIGHT = r"(?![\w.])"


def key_pattern(key: str) -> re.Pattern[str]:
    """A compiled pattern matching *key* as a whole config key."""
    return re.compile(f"{_LEFT}{re.escape(key)}{_RIGHT}")


def names_key(text: str, key: str) -> bool:
    """Whether *text* names *key* as a key rather than as part of one."""
    return key_pattern(key).search(text) is not None


def named_keys(text: str, keys: Iterable[str]) -> list[str]:
    """The subset of *keys* that *text* names as keys, sorted."""
    return sorted({key for key in keys if names_key(text, key)})
