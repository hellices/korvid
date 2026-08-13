"""Helpers shared by the observability connector tests."""

from __future__ import annotations

import re


def skeleton(query: str) -> str:
    """`query` with every complete string literal collapsed to `""`.

    What remains is the query's structure. A value that stayed inside its
    literal contributes nothing to it; a value that broke out does, which
    is exactly the property the injection tests need to measure.
    """
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', query)
