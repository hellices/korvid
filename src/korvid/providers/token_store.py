"""OS-keyring-backed token storage with a 0600 JSON file fallback."""

from __future__ import annotations

import json
import logging
import os
from os import fsync as os_fsync
from os import replace as os_replace
from pathlib import Path
from tempfile import mkstemp

logger = logging.getLogger(__name__)

_SERVICE = "korvid"
# Fallback-file marker recording a delete whose keyring removal failed;
# load() honors it (never resurrects the token) and retries the cleanup.
_PENDING_DELETE = "__pending_delete__:"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "korvid" / "credentials.json"


class TokenStore:
    def __init__(self, fallback_path: Path | None = None) -> None:
        self._path = fallback_path or DEFAULT_CREDENTIALS_PATH

    def save(self, key: str, value: str) -> None:
        try:
            import keyring

            keyring.set_password(_SERVICE, key, value)
        except Exception:
            logger.debug("keyring unavailable; using file fallback", exc_info=True)
            # Write the file copy first so at least one copy always survives;
            # only then best-effort delete the stale keyring entry so load()
            # cannot resurrect an older token once keyring recovers.
            data = self._read_file()
            data[key] = value
            data.pop(_PENDING_DELETE + key, None)  # a fresh save overrides it
            self._write_file(data)
            try:
                import keyring

                keyring.delete_password(_SERVICE, key)
            except Exception:
                logger.debug("stale keyring entry cleanup failed", exc_info=True)
            return
        # Keyring save succeeded: drop any stale file copy (and any pending
        # deletion marker) so neither can shadow the fresh keyring value.
        data = self._read_file()
        if key in data or _PENDING_DELETE + key in data:
            data.pop(key, None)
            data.pop(_PENDING_DELETE + key, None)
            self._write_file(data)

    def load(self, key: str) -> str | None:
        data = self._read_file()
        if _PENDING_DELETE + key in data:
            # A previous delete() could not remove the keyring entry.
            # Retry the cleanup; until it succeeds the token stays deleted.
            if not self._keyring_delete(key):
                return None
            del data[_PENDING_DELETE + key]
            self._write_file(data)
            return data.get(key)
        try:
            import keyring

            value = keyring.get_password(_SERVICE, key)
            if value is not None:
                return value
        except Exception:
            logger.debug("keyring unavailable; using file fallback", exc_info=True)
        return data.get(key)

    def delete(self, key: str) -> None:
        deleted = self._keyring_delete(key)
        data = self._read_file()
        changed = key in data or _PENDING_DELETE + key in data
        data.pop(key, None)
        data.pop(_PENDING_DELETE + key, None)
        if not deleted:
            # Record the pending deletion so a recovered keyring backend
            # cannot resurrect the token through load().
            data[_PENDING_DELETE + key] = "1"
            changed = True
        if changed:
            self._write_file(data)

    def _keyring_delete(self, key: str) -> bool:
        """Remove the keyring entry; True when it is gone (deleted or absent)."""
        try:
            import keyring

            keyring.delete_password(_SERVICE, key)
        except ImportError:
            return True  # no keyring backend -> nothing stored there
        except Exception as exc:
            # keyring.errors.PasswordDeleteError means the entry does not
            # exist, which is a successful deletion. Matched by name so a
            # broken keyring install cannot break the error path itself.
            if type(exc).__name__ == "PasswordDeleteError":
                return True
            logger.debug("keyring delete failed", exc_info=True)
            return False
        return True

    def _read_file(self) -> dict[str, str]:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

    def _write_file(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Unique same-directory temp file + fsync + atomic replace: an
        # interrupted write can never truncate the only stored credential
        # copy, a power loss cannot leave an empty file, and concurrent
        # writers cannot race on a shared temp name. mkstemp creates 0600.
        fd, tmp_name = mkstemp(dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            # Write through the mkstemp fd and fsync it while still writable:
            # Windows' fsync (_commit) rejects read-only handles.
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data))
                fh.flush()
                os_fsync(fh.fileno())
            os_replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)
