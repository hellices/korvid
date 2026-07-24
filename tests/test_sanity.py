"""Gate-stack sanity: the package imports and has a version."""

import korvid


def test_version() -> None:
    assert korvid.__version__ == "0.1.0"
