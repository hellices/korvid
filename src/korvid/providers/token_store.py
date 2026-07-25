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
            self._write_file(data)
            try:
                import keyring

                keyring.delete_password(_SERVICE, key)
            except Exception:
                logger.debug("stale keyring entry cleanup failed", exc_info=True)
            return
        # Keyring save succeeded: drop any stale file copy so it can never
        # shadow the fresh keyring value after a later keyring outage.
        data = self._read_file()
        if key in data:
            del data[key]
            self._write_file(data)

    def load(self, key: str) -> str | None:
        try:
            import keyring

            value = keyring.get_password(_SERVICE, key)
            if value is not None:
                return value
        except Exception:
            logger.debug("keyring unavailable; using file fallback", exc_info=True)
        return self._read_file().get(key)

    def delete(self, key: str) -> None:
        try:
            import keyring

            keyring.delete_password(_SERVICE, key)
        except Exception:
            logger.debug("keyring delete failed or unavailable", exc_info=True)
        data = self._read_file()
        if key in data:
            del data[key]
            self._write_file(data)

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
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(json.dumps(data))
            fsync_fd = os.open(tmp, os.O_RDONLY)
            try:
                os_fsync(fsync_fd)
            finally:
                os.close(fsync_fd)
            os_replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)
