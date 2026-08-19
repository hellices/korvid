from __future__ import annotations

from korvid import __version__


def isolated_install_hint(*, entra: bool = False) -> str:
    extras = "all,entra" if entra else "all"
    requirement = f"korvid[{extras}]=={__version__}"
    return (
        "reinstall with: "
        f"uv tool install --force '{requirement}' "
        f"(or: pipx install --force '{requirement}')"
    )
