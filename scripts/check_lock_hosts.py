"""Assert a lockfile resolves from PyPI and nowhere else.

Used by the Relock workflow, which cannot trust the file it is handed: the
job that produced it also ran the dependencies it resolved, and any of them
could have replaced `uv.lock` afterwards.

Parsing rather than grepping, because a text match over an unknown file
fails *open*. `registry="https://evil.example/simple"` - valid TOML, no
spaces around the `=` - matches no reasonable line pattern, and neither does
an empty file; a grep-based check reports success on both. Every failure
here is explicit instead.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: The only hosts a lock may name.
ALLOWED_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})

#: Keys whose values are URLs. `uv` writes `url` for an artifact and
#: `registry` for the index that served it; `index` appears when a project
#: pins one, which this repository never does.
URL_KEYS = frozenset({"url", "registry", "index"})


def _walk(node: Any) -> list[tuple[str, str]]:
    """Every `(key, url)` pair anywhere in the parsed document."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in URL_KEYS and isinstance(value, str):
                found.append((key, value))
            else:
                found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def check(path: Path) -> list[str]:
    """Return the reasons `path` is not an acceptable lock; empty means good."""
    try:
        document = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"{path} is not readable TOML: {exc}"]

    urls = _walk(document)
    if not urls:
        # A lock with no URLs at all is not a lock that passed - it is a
        # file this check cannot say anything about, which is the case a
        # text match would have waved through.
        return [f"{path} names no package URLs; refusing to treat that as verified"]

    problems: list[str] = []
    for key, url in sorted(set(urls)):
        parts = urlsplit(url)
        if parts.scheme != "https":
            problems.append(f"{key} = {url!r} is not https")
        elif parts.hostname not in ALLOWED_HOSTS:
            problems.append(f"{key} = {url!r} names {parts.hostname}")
    return problems


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "uv.lock")
    problems = check(path)
    if problems:
        print(f"{path} does not resolve from PyPI only:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"{path} resolves from PyPI only")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through tests
    raise SystemExit(main(sys.argv))
