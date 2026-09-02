"""Column sorting (issue #37) — the rendered-outcome coverage now lives at
the pilot boundary in `tests/ui/test_column_sorting.py`, which drives
name/age/cpu/mem/custom-column sorting via real keypresses against rendered
table content. This file keeps only the one contract that boundary cannot
reach: `sort_rows` is a public function whose docstring documents a
`Raises: ValueError` contract for an unsupported column, and every UI caller
only ever passes UI-validated column names — so nothing else in the suite
exercises this branch at all.
"""

from __future__ import annotations

import pytest

from korvid.core.sorting import SortSpec, sort_rows
from korvid.k8s.models import PodSummary


def test_sort_rows_rejects_unsupported_column() -> None:
    """Without validation an unknown column would silently fall through to
    the metrics branch and return a plausible but wrong order."""
    pod = PodSummary(
        name="a", namespace="default", phase="Running", ready="1/1", restarts=0, node="n1"
    )
    with pytest.raises(ValueError, match="unsupported sort column"):
        sort_rows([pod], SortSpec("restarts", descending=True))
