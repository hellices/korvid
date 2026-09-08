"""Optional, bounded metadata enrichment from models.dev.

Contract (design §Model Catalog Architecture, layer 2):

- Never fetched at startup, never awaited on any hot path.
- Never carries a credential, a prompt, a tool argument, a model
  reference the operator has selected, or any other korvid state — the
  request is a bare conditional GET of one public document.
- Never influences routing. It may add a description, a release date or
  a credential-variable *hint*; it can never change which endpoint a
  request goes to or which parameters are sent.
- A failure is silent and total: the catalog falls back to the cache,
  then to LiteLLM's bundled tables, and korvid stays fully usable.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from tempfile import mkstemp
from typing import Any, Final

#: One conditional GET of one public document. No query string, ever.
MODELS_DEV_URL: Final[str] = "https://models.dev/api.json"

#: The document measured 4,473,344 bytes on 2026-09-05. The ceiling is a
#: little under 3x that, so ordinary growth does not trip it but a
#: redirect to something unbounded does.
MAX_RESPONSE_BYTES: Final[int] = 12 * 1024 * 1024

#: Whole-request budget: connect, headers, body and parse together, not
#: per socket operation. Enrichment is never worth making a human wait.
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0

#: Revalidate at most daily; serve the cache unconditionally in between.
CACHE_TTL_SECONDS: Final[int] = 24 * 60 * 60

CACHE_FILENAME: Final[str] = "models-dev.json"

#: The POSIX mode a freshly written cache envelope carries: readable and
#: writable by its owner, by nobody else. Enforced on the staging
#: descriptor rather than left to `mkstemp`, whose 0600 a umask can still
#: subtract from.
#:
#: Windows has no equivalent to assert. A `mode` there can only clear the
#: read-only attribute, `st_mode` is synthesised by the CRT (every
#: writable file reads back `0o666`), and access is decided by the NTFS
#: ACL the file inherits from its parent directory — the per-user
#: `%LOCALAPPDATA%` tree `default_cache_path` selects. That inheritance
#: is why `_open_staging_file` stages *within* the cache directory and
#: `_write_envelope` renames within it, instead of anywhere shared.
CACHE_FILE_MODE: Final[int] = 0o600


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """The subset korvid renders. Everything else is discarded on parse."""

    reference: str
    display_name: str | None = None
    description: str | None = None
    release_date: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    credential_env_hints: tuple[str, ...] = ()


class ModelMetadataSource(ABC):
    """What the catalog depends on. Keeps HTTP out of the catalog."""

    @abstractmethod
    def metadata(self, reference: str) -> ModelMetadata | None: ...

    @abstractmethod
    def env_hints(self, provider_id: str) -> tuple[str, ...]: ...

    @abstractmethod
    async def refresh(self, *, force: bool = False) -> RefreshOutcome:
        """Revalidate this source, on an explicit operator request only.

        On the ABC rather than only on the concrete class: the catalog
        holds sources by this type, and the setup UI's refresh action has
        to reach one through it. A source that has nothing to revalidate
        answers `CACHED` — never by omitting the method, which would make
        the action's reachability depend on the runtime type it happened
        to be given.

        Args:
            force: `True` when a human asked for this refresh and is
                waiting for the answer. A source that serves a local copy
                inside a freshness window must go and look anyway — the
                window is korvid's own restraint, and the operator has
                just overridden it. The default keeps every other caller
                cache-first.
        """


class RefreshOutcome(Enum):
    UPDATED = "updated"
    NOT_MODIFIED = "not-modified"
    CACHED = "cached"  # TTL not expired; no request made
    UNAVAILABLE = "unavailable"  # network/parse failure; stale data kept


def default_cache_path() -> Path:
    """`$XDG_CACHE_HOME/korvid/models-dev.json`, falling back to the
    platform convention: `~/Library/Caches` on macOS,
    `%LOCALAPPDATA%` on Windows, `~/.cache` elsewhere.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "korvid" / CACHE_FILENAME

    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Caches"
    elif platform.system() == "Windows":
        local_app = os.environ.get("LOCALAPPDATA")
        base = Path(local_app) if local_app else Path.home() / "AppData" / "Local"
    else:
        base = Path.home() / ".cache"

    return base / "korvid" / CACHE_FILENAME


def _open_staging_file(path: Path) -> tuple[int, Path]:
    """Create the file the cache envelope is written to before it is
    renamed onto *path*, and return its descriptor and name.

    A *unique sibling*, and both halves carry a guarantee.

    Sibling: the staging file inherits its access control from the
    directory it is created in, and `os.replace` inside one directory
    keeps it; creating it anywhere shared — or renaming across
    directories — would hand the envelope whatever protection that other
    place has. On Windows that inheritance *is* the protection, because
    there are no mode bits to set (see `CACHE_FILE_MODE`).

    Unique: a fixed name is a name something else can hold first. Two
    korvid processes refreshing at once would interleave into one file
    and publish a torn envelope built from both, and an `O_CREAT` open of
    a name that already exists ignores the mode it was asked for — so a
    pre-created, world-readable file would have kept those permissions
    straight through the rename. `mkstemp` opens with `O_CREAT | O_EXCL`,
    so it never adopts a file it did not make.

    Args:
        path: Where the envelope will finally live.

    Returns:
        An open, writable descriptor and the staging file's own path. The
        caller owns both: it must close the descriptor and remove the
        file on every path that does not rename it away.
    """
    fd, name = mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    if os.name == "nt":
        # No POSIX bits to set; confidentiality is the directory's ACL.
        return fd, tmp
    try:
        # `mkstemp` already creates 0600, but a umask subtracts from that
        # and `CACHE_FILE_MODE` is published as the exact mode. Applied
        # to the descriptor, so no other name can be chmod'ed instead.
        os.fchmod(fd, CACHE_FILE_MODE)
    except OSError:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    return fd, tmp


def _positive_int(value: object) -> int | None:
    """Return *value* only when it is a positive integer.

    Rejects `bool` (an `int` subclass) and non-positive values.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _strict_bool(value: object) -> bool | None:
    """Return *value* only when it is exactly `True` or `False`."""
    if value is True or value is False:
        return value
    return None


def _parse(
    document: object,
) -> tuple[dict[str, ModelMetadata], dict[str, tuple[str, ...]]]:
    """Parse the models.dev document into metadata and env-hints tables.

    Validates per entry and drops anything that does not fit, rather than
    rejecting the document. The top-level object must be a dict; anything
    else raises `ValueError`.
    """
    if not isinstance(document, dict):
        raise ValueError("models.dev document must be an object")

    metadata: dict[str, ModelMetadata] = {}
    env_hints: dict[str, tuple[str, ...]] = {}

    for provider_id, provider_val in document.items():
        if not isinstance(provider_val, dict):
            continue

        # Collect env hints for this provider.
        raw_env = provider_val.get("env")
        if isinstance(raw_env, list):
            hints = tuple(v for v in raw_env if isinstance(v, str) and v)
            if hints:
                env_hints[provider_id] = hints

        models_val = provider_val.get("models")
        if not isinstance(models_val, dict):
            continue

        for model_id, model_val in models_val.items():
            if not isinstance(model_val, dict):
                continue

            reference = f"{provider_id}/{model_id}"

            # display_name falls back to model id.
            raw_name = model_val.get("name")
            display_name: str = raw_name if isinstance(raw_name, str) and raw_name else model_id

            raw_desc = model_val.get("description")
            description = raw_desc if isinstance(raw_desc, str) else None

            raw_date = model_val.get("release_date")
            release_date = raw_date if isinstance(raw_date, str) else None

            limit = model_val.get("limit")
            context_window: int | None = None
            max_output: int | None = None
            if isinstance(limit, dict):
                context_window = _positive_int(limit.get("context"))
                max_output = _positive_int(limit.get("output"))

            supports_tools = _strict_bool(model_val.get("tool_call"))
            supports_reasoning = _strict_bool(model_val.get("reasoning"))

            metadata[reference] = ModelMetadata(
                reference=reference,
                display_name=display_name,
                description=description,
                release_date=release_date,
                context_window_tokens=context_window,
                max_output_tokens=max_output,
                supports_tools=supports_tools,
                supports_reasoning=supports_reasoning,
                credential_env_hints=env_hints.get(provider_id, ()),
            )

    return metadata, env_hints


def _cache_age(envelope: dict[str, Any], now: float) -> float | None:
    """How long ago *envelope* was written, or None when it cannot say.

    The cache file lives in a user-writable directory, so its fields are
    input rather than korvid's own state. `fetched_at` is read by the
    freshness check — the one step of a refresh that runs *before* the
    error handling — so anything that is not a finite, non-future number
    has to be answered here instead of thrown out of a method documented
    as never raising. An unusable timestamp is not evidence of freshness:
    the caller revalidates, which is what an aged-out cache does anyway.
    """
    value = envelope.get("fetched_at")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    age = now - float(value)
    # A future timestamp is either a tamper or a clock that moved; either
    # way, honouring it would pin korvid to this envelope until it passes.
    if not math.isfinite(age) or age < 0:
        return None
    return age


def _cache_etag(envelope: dict[str, Any]) -> str | None:
    """The envelope's validator, or None when it is not one.

    A non-string `etag` cannot become an `if-none-match` header. Sending
    it would raise inside the request and cost the operator the refresh,
    so it is treated as absent and the GET goes out unconditional.
    """
    value = envelope.get("etag")
    return value if isinstance(value, str) else None


def _default_client_factory(ca_bundle: str | None = None) -> AbstractAsyncContextManager[Any]:
    """The production client: korvid's own trust, nothing else.

    Built through `net.make_client`, so `network.ca_bundle` reaches this
    request exactly as it reaches the live providers and the wizard's
    probe — a deployment behind a TLS-inspecting proxy either trusts all
    of them or none of them. There is no insecure mode to fall back to:
    an unreadable or malformed bundle raises, and `refresh` reports the
    refresh as unavailable rather than retrying without verification.
    """
    try:
        from korvid.providers import net
    except ImportError as exc:
        raise ImportError(
            "httpx is required for models.dev enrichment. Install korvid[agent] or korvid[mcp]."
        ) from exc
    return net.make_client(ca_bundle, timeout=REQUEST_TIMEOUT_SECONDS)


class ModelsDevSource(ModelMetadataSource):
    """Fetch, cache, and expose models.dev metadata.

    Args:
        cache_path: Where to store the cache envelope. Defaults to the
            platform cache directory.
        client_factory: Callable that returns an async HTTP client context
            manager. Defaults to a `network.ca_bundle`-aware client. An
            injected factory is the test seam and wins outright — the
            bundle is then that factory's business.
        clock: Time source (injectable for tests).
        ca_bundle: `network.ca_bundle`. One trust decision for every
            korvid-owned HTTPS client; this one is no exception.
    """

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        client_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        clock: Callable[[], float] = time.time,
        ca_bundle: str | None = None,
    ) -> None:
        self._cache_path = cache_path or default_cache_path()
        # Bound, not called: the client is built when an operator asks for
        # a refresh, so startup neither opens a socket nor reads the CA
        # bundle off disk.
        self._client_factory = client_factory or partial(_default_client_factory, ca_bundle)
        self._clock = clock
        self._metadata: dict[str, ModelMetadata] = {}
        self._env_hints: dict[str, tuple[str, ...]] = {}
        self._loaded = False
        #: Serialises refreshes so concurrent callers are one request out.
        self._refresh_lock = asyncio.Lock()
        #: Bumped by every completed refresh, with its outcome, so a caller
        #: that waited can report what it waited for.
        self._completed = 0
        self._last_outcome: RefreshOutcome | None = None
        # Try to load from cache on construction (sync, best-effort).
        self._load_cache()

    # ------------------------------------------------------------------
    # ModelMetadataSource interface
    # ------------------------------------------------------------------

    def metadata(self, reference: str) -> ModelMetadata | None:
        """Return metadata for an exact reference, or None. Never raises."""
        return self._metadata.get(reference)

    def env_hints(self, provider_id: str) -> tuple[str, ...]:
        """Return credential env-var hints for a provider. Never raises."""
        return self._env_hints.get(provider_id, ())

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def refresh(self, *, force: bool = False) -> RefreshOutcome:
        """Revalidate, on an explicit request or when the cache has aged out.

        Called only from the setup UI's 'refresh model metadata' action —
        never at startup.

        Args:
            force: `True` when a human pressed the refresh key. The 24-hour
                freshness window is korvid's restraint about how often it
                will contact models.dev on its own; an operator who asked
                has overridden it, and answering `CACHED` would make the
                only control they have over this layer do nothing. The
                request is still conditional, so forcing costs a round trip
                and an ETag comparison, not 4 MiB of unchanged document.
        """
        # Read before waiting: if the counter has moved by the time this
        # call holds the lock, somebody else's refresh is the answer.
        completed_before = self._completed
        async with self._refresh_lock:
            joined = self._joinable_outcome(completed_before, force=force)
            if joined is not None:
                return joined
            outcome = await self._refresh_now(force=force)
            self._completed += 1
            self._last_outcome = outcome
            return outcome

    def _joinable_outcome(self, completed_before: int, *, force: bool) -> RefreshOutcome | None:
        """The outcome of a refresh that landed while this call waited.

        Two screens — or one held key that outran a screen's own guard —
        must not become two requests to models.dev. Once `force` bypasses
        the TTL, the TTL is no longer what stops korvid repeating itself,
        so the caller that arrives second reports what the first one got.

        A `CACHED` answer is never joinable by a forced caller: the other
        caller never went to the network, so joining it would re-introduce
        exactly the short-circuit `force` exists to bypass.
        """
        if self._completed == completed_before or self._last_outcome is None:
            return None
        if force and self._last_outcome is RefreshOutcome.CACHED:
            return None
        return self._last_outcome

    async def _refresh_now(self, *, force: bool) -> RefreshOutcome:
        """One revalidation, under one deadline. Never raises."""
        cached = self._read_envelope()
        now = self._clock()

        if not force and cached is not None:
            age = _cache_age(cached, now)
            if age is not None and age < CACHE_TTL_SECONDS:
                return RefreshOutcome.CACHED

        etag: str | None = _cache_etag(cached) if cached is not None else None

        try:
            # One deadline over connect, headers, body and parse. An HTTP
            # client's own timeout is spent *per operation*, so a server
            # that answers every individual read inside the limit — a
            # slow drip, a stalled proxy — can hold an operator's refresh
            # open for as long as it likes. The documented budget is a
            # whole-request budget, so it is enforced as one.
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                return await self._revalidate(etag=etag, cached=cached, now=now)
        except TimeoutError:
            # Same answer as a refused connection: the stale cache and the
            # in-memory tables both stand, and korvid stays usable.
            return RefreshOutcome.UNAVAILABLE
        except Exception:
            # A misconfigured `network.ca_bundle` lands here too: trust is
            # never downgraded to get an answer, and enrichment is not
            # worth failing a running TUI over.
            return RefreshOutcome.UNAVAILABLE

    async def _revalidate(
        self, *, etag: str | None, cached: dict[str, Any] | None, now: float
    ) -> RefreshOutcome:
        """Fetch, parse and store, under the caller's deadline."""
        result = await self._fetch(etag)

        if result is None:
            # 304 Not Modified — touch the timestamp to reset TTL.
            if cached is not None:
                self._write_envelope(
                    {"fetched_at": now, "etag": etag, "document": cached.get("document", {})}
                )
            return RefreshOutcome.NOT_MODIFIED

        body, new_etag = result
        try:
            document = json.loads(body)
            new_meta, new_hints = _parse(document)
        except (json.JSONDecodeError, ValueError):
            return RefreshOutcome.UNAVAILABLE

        self._write_envelope({"fetched_at": now, "etag": new_etag, "document": document})
        self._metadata = new_meta
        self._env_hints = new_hints
        return RefreshOutcome.UPDATED

    async def _fetch(self, etag: str | None) -> tuple[bytes, str | None] | None:
        """Perform a bounded conditional GET.

        Returns `None` on 304, `(body, etag)` on 200, raises on error.
        """

        headers: dict[str, str] = {"accept": "application/json"}
        if etag:
            headers["if-none-match"] = etag
        async with (
            self._client_factory() as client,
            client.stream(
                "GET",
                MODELS_DEV_URL,
                headers=headers,
                # A per-operation ceiling under `refresh`'s total deadline:
                # it fails a single stalled socket call fast, but only the
                # outer `asyncio.timeout` bounds the whole request.
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as response,
        ):
            if response.status_code == 304:
                return None
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";")[0].strip()
            if media_type != "application/json":
                raise ValueError(f"unexpected content type: {media_type!r}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeded the size ceiling")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("etag")

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        """Load metadata from the cache file if present. Best-effort."""
        envelope = self._read_envelope()
        if envelope is None:
            return
        try:
            document = envelope.get("document", {})
            meta, hints = _parse(document)
            self._metadata = meta
            self._env_hints = hints
        except (ValueError, TypeError):
            pass

    def _read_envelope(self) -> dict[str, Any] | None:
        """Read the cache envelope. Returns None on any error."""
        try:
            text = self._cache_path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _write_envelope(self, envelope: dict[str, Any]) -> None:
        """Write the cache envelope atomically, owner-only.

        Unique same-directory staging file, fsync, atomic replace: an
        interrupted write can never leave a half-parsed envelope behind,
        a power loss cannot leave an empty one, and two processes
        refreshing at once cannot race on a shared name. The mode is
        `CACHE_FILE_MODE` where a mode means something; on Windows the
        sibling relationship, not the mode, is what keeps the envelope
        inside the per-user directory's access control.

        Raises:
            OSError: The envelope could not be written or renamed into
                place. The staging file is removed either way.
        """
        path = self._cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _open_staging_file(path)
        try:
            # Write through the staging descriptor and fsync it while
            # still writable: Windows' fsync (_commit) rejects read-only
            # handles.
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            # Every error, not only the ones raised while writing: a
            # staging file left behind is a readable copy of the envelope
            # under a name nothing ever cleans up.
            tmp.unlink(missing_ok=True)
