"""Bearer-token resolution for observability connectors (issue #193).

A token is named in config, never stored there, and never held on the
connector: it is read at call time, used in one header, and dropped. That
is why an expiring token works without restarting korvid, and why no
result, error message or audit record can carry one.
"""

from __future__ import annotations

import asyncio
import os
from stat import S_ISREG

from korvid.obs.connector import ConnectorError

#: A bearer token is placed in an HTTP header verbatim. Anything outside
#: printable US-ASCII makes the header illegal, and httpx reports an
#: illegal header by quoting **the value** - so a token with an embedded
#: CR would arrive in a tool result inside the exception text. Refusing
#: it here is what makes "the token never appears in an error" true.
_HEADER_SAFE = frozenset(chr(code) for code in range(0x20, 0x7F))


def _header_safe(token: str) -> bool:
    return all(char in _HEADER_SAFE for char in token)


def require_header_safe(value: str, what: str, source: str) -> str:
    """`value`, or a refusal naming `what` but never the value itself.

    Used for every caller-supplied header value, not only the bearer
    token: an illegal header makes the HTTP client raise an error quoting
    the value, and that error becomes a tool result (PR #280 review).
    """
    if not _header_safe(value):
        raise ConnectorError(
            "config",
            f"{source}: {what} is not a valid HTTP header value"
            f" (it contains a control or non-ASCII character)",
        )
    return value


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
            value = _read_token_file(token_file)
        except OSError as exc:
            raise ConnectorError(
                "config", f"{source}: token file {token_file!r} could not be read: {exc.strerror}"
            ) from exc
        except ValueError as exc:
            # UnicodeDecodeError is a ValueError, not an OSError: a binary
            # or non-UTF-8 file would otherwise escape as an unexpected
            # exception rather than an actionable config error. The size
            # bound raises ValueError too, and says so.
            detail = str(exc)
            if "larger than" in detail:
                reason = f"is too large ({detail})"
            elif "regular file" in detail:
                reason = "is not a regular file"
            else:
                reason = "could not be read as UTF-8 text"
            raise ConnectorError("config", f"{source}: token file {token_file!r} {reason}") from exc
        token = value.strip()
        if not token:
            raise ConnectorError("config", f"{source}: token file {token_file!r} is empty")
        return _validated(token, f"token file {token_file!r}", source)
    return None


#: A bearer token is a header value; anything approaching this size is a
#: mistake, a device, or a runaway file. Reading it in full would be the
#: first thing to go wrong (PR #280 review).
MAX_TOKEN_FILE_BYTES = 64 * 1024


def _read_token_file(token_file: str) -> str:
    """The token file's text, bounded.

    The file must be a regular file, checked before it is opened: opening
    a FIFO with no writer blocks forever, and the worker thread this runs
    on cannot be cancelled — the timeout would return while the thread
    stayed occupied, and enough of those exhaust the executor (PR #280
    review). A stalled network mount can still hold a worker; that is a
    residual, and it needs no special file type to reach.

    Raises:
        OSError: the file could not be opened or read.
        ValueError: it is not a regular file, its contents are not UTF-8,
            or it is too large.
    """
    if not S_ISREG(os.stat(token_file).st_mode):
        raise ValueError("is not a regular file")
    with open(token_file, "rb") as handle:  # bounded read, not a path helper
        raw = handle.read(MAX_TOKEN_FILE_BYTES + 1)
    if len(raw) > MAX_TOKEN_FILE_BYTES:
        raise ValueError(f"larger than {MAX_TOKEN_FILE_BYTES} bytes")
    return raw.decode("utf-8")


async def resolve_token_async(
    *, token_env: str | None, token_file: str | None, source: str, getenv: object = None
) -> str | None:
    """`resolve_token`, with the file read moved off the event loop.

    A synchronous read of a FIFO, a device, or a file on a stalled network
    mount holds the loop, and the `asyncio.timeout` that is supposed to
    bound the call cannot fire while it does — so the one guarantee the
    timeout exists to make would be the one it could not keep.
    """
    if token_file and not token_env:
        return await asyncio.to_thread(
            resolve_token,
            token_env=token_env,
            token_file=token_file,
            source=source,
            getenv=getenv,
        )
    return resolve_token(token_env=token_env, token_file=token_file, source=source, getenv=getenv)


def _validated(token: str, where: str, source: str) -> str:
    """`token`, or a refusal that names where it came from but not what it is."""
    return require_header_safe(token, f"the token in {where}", source)
