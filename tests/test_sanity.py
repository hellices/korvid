"""Gate-stack sanity: the package imports and has a version."""

import re

import korvid


def test_version() -> None:
    """Only the shape is checked here.

    `__version__` is the string the release workflow matches the pushed tag
    against, and `test_pyproject_version_matches_the_package_version` already
    pins it to `pyproject.toml`. Repeating the literal in a third place makes
    a version bump a five-file edit and buys nothing.
    """
    assert re.fullmatch(r"\d+\.\d+\.\d+", korvid.__version__), korvid.__version__
