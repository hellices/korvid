"""Runtime guidance for reinstalling missing optional extras safely."""

from __future__ import annotations

from korvid import __version__


def isolated_install_hint(*, feature: str, requirement_extras: str = "all") -> str:
    """Build an isolated reinstall hint for the requested feature.

    Args:
        feature: Missing feature to name in the user-facing guidance.
        requirement_extras: Exact extras string that must be reinstalled.

    Returns:
        A user-facing hint that preserves the requested requirement and keeps
        the reinstall inside an isolated tool-managed environment.
    """
    requirement = f"korvid[{requirement_extras}]=={__version__}"
    return (
        f"reinstall the complete extras you use (including {feature}) with: "
        f"uv tool install --force '{requirement}' "
        f"(or: pipx install --force '{requirement}'). "
        "For a development checkout or active virtualenv, reinstall the "
        "complete extras in that environment instead."
    )
