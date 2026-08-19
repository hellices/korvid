"""Runtime guidance for reinstalling missing optional extras safely."""

from __future__ import annotations

from korvid import __version__


def isolated_install_hint(*, extras: str = "all") -> str:
    """Build an isolated reinstall hint for the requested extra set.

    Args:
        extras: Exact extras string that must be reinstalled.

    Returns:
        A user-facing hint that preserves the requested extras and keeps the
        reinstall inside an isolated tool-managed environment.
    """
    requirement = f"korvid[{extras}]=={__version__}"
    return (
        "reinstall with: "
        f"uv tool install --force '{requirement}' "
        f"(or: pipx install --force '{requirement}'). "
        "For a development checkout or active virtualenv, reinstall the "
        "complete extras in that environment instead."
    )
