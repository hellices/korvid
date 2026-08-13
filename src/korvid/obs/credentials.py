"""Bearer-token resolution for observability connectors (issue #193).

A token is named in config, never stored there, and never held on the
connector: it is read at call time, used in one header, and dropped. That
is why an expiring token works without restarting korvid, and why no
result, error message or audit record can carry one.
"""

from __future__ import annotations

from pathlib import Path

from korvid.obs.connector import ConnectorError


def resolve_token(
    *, token_env: str | None, token_file: str | None, source: str, getenv: object = None
) -> str | None:
    """The bearer token for this call, or None when none is configured.

    Args:
        token_env: Environment variable name holding the token.
        token_file: Path to a file holding the token.
        source: Connector name, for the error message.
        getenv: Environment lookup, injectable for tests. Defaults to
            `os.environ.get`.

    Returns:
        The token, or None when neither source is configured.

    Raises:
        ConnectorError: `config` when a configured source is empty,
            missing or unreadable. The message names the variable or path
            and never the value.
    """
    import os

    lookup = getenv if callable(getenv) else os.environ.get
    if token_env and token_file:
        raise ConnectorError(
            "config",
            f"{source}: token_env and token_file are both set;"
            f" configure exactly one credential source",
        )
    if token_env:
        value = lookup(token_env)
        if not value:
            raise ConnectorError(
                "config",
                f"{source}: environment variable {token_env} is unset or empty",
            )
        return str(value).strip()
    if token_file:
        try:
            value = Path(token_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConnectorError(
                "config", f"{source}: token file {token_file!r} could not be read: {exc.strerror}"
            ) from exc
        token = value.strip()
        if not token:
            raise ConnectorError("config", f"{source}: token file {token_file!r} is empty")
        return token
    return None
