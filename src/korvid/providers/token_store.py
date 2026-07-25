"""OS-keyring-backed token storage with a 0600 JSON file fallback."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

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
            return
        except Exception:
            logger.debug("keyring unavailable; using file fallback", exc_info=True)
        data = self._read_file()
        data[key] = value
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
        self._path.touch(exist_ok=True)
        os.chmod(self._path, 0o600)
        self._path.write_text(json.dumps(data))
