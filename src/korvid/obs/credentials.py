"""Bearer-token resolution for observability connectors (issue #193).

A token is named in config, never stored there, and never held on the
connector: it is read at call time, used in one header, and dropped. That
is why an expiring token works without restarting korvid, and why no
result, error message or audit record can carry one.
"""

from __future__ import annotations

from pathlib import Path

from korvid.obs.connector import ConnectorError

#: A bearer token is placed in an HTTP header verbatim. Anything outside
#: printable US-ASCII makes the header illegal, and httpx reports an
#: illegal header by quoting **the value** - so a token with an embedded
#: CR would arrive in a tool result inside the exception text. Refusing
#: it here is what makes "the token never appears in an error" true.
_HEADER_SAFE = frozenset(chr(code) for code in range(0x20, 0x7F))


def _header_safe(token: str) -> bool:
    return all(char in _HEADER_SAFE for char in token)


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
        raw = lookup(token_env)
        # Stripped *before* the emptiness check: a variable holding only
        # whitespace is truthy, and would otherwise send `Bearer ` - an
        # unauthenticated request that looks configured.
        token = str(raw).strip() if raw is not None else ""
        if not token:
            raise ConnectorError(
                "config",
                f"{source}: environment variable {token_env} is unset or empty",
            )
        return _validated(token, f"environment variable {token_env}", source)
    if token_file:
        try:
            value = Path(token_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConnectorError(
                "config", f"{source}: token file {token_file!r} could not be read: {exc.strerror}"
            ) from exc
        except ValueError as exc:
            # UnicodeDecodeError is a ValueError, not an OSError: a binary
            # or non-UTF-8 file would otherwise escape as an unexpected
            # exception rather than an actionable config error.
            raise ConnectorError(
                "config", f"{source}: token file {token_file!r} could not be read as UTF-8 text"
            ) from exc
        token = value.strip()
        if not token:
            raise ConnectorError("config", f"{source}: token file {token_file!r} is empty")
        return _validated(token, f"token file {token_file!r}", source)
    return None


def _validated(token: str, where: str, source: str) -> str:
    """`token`, or a refusal that names where it came from but not what it is."""
    if not _header_safe(token):
        raise ConnectorError(
            "config",
            f"{source}: the token in {where} is not a valid HTTP header value"
            f" (it contains a control or non-ASCII character)",
        )
    return token
