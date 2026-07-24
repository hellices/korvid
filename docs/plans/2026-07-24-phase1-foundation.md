# korvid Phase 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the korvid foundation — repo gates, config, k8s client wrapper, resource store/watch, Textual app shell with UI Bus, and a pods view vertical slice with `:` command bar and `/` filter.

**Architecture:** Pragmatic layered per `docs/specs/2026-07-24-korvid-engineering-standards.md` §3 — `ui/` (Textual, owns Messages) → `core/` + `agent/` + `k8s/` (pure Python, Textual-free), constructor injection wired in `__main__.py`, layer rules enforced by tach.

**Tech Stack:** Python ≥3.11, Textual ≥8, kubernetes_asyncio, PyYAML, uv + hatchling, Ruff, mypy --strict, pytest + pytest-asyncio + Pilot.

## Global Constraints

- Python `requires-python = ">=3.11"`, line-length 100, license Apache-2.0
- All tool config in `pyproject.toml` (exceptions: `.pre-commit-config.yaml`, `tach.toml`, workflows)
- Layer import rules (tach-enforced): `ui→{core,agent,k8s}`, `core→k8s`, `agent→{core,k8s}`, `k8s→(stdlib+client)`, `providers→agent`; Textual imports **only** in `ui/`
- mypy --strict passes; no bare `# type: ignore`; ruff `select` per standards §1; complexity ≤ 10
- Every test asserts; `pytest.raises` uses `match=`; warnings are errors
- Commit after every green task; never `--no-verify`
- Agent design (§6 of design doc) is OUT of scope for this plan — but the `agent/` dir and `LLMProvider` ABC placeholder are created so tach rules are complete from day one

---

### Task 1: Repository gates scaffold

**Files:**
- Create: `pyproject.toml`, `tach.toml`, `.pre-commit-config.yaml`, `Makefile`, `.github/workflows/ci.yml`, `.claude/settings.json`, `.claude/hooks/protect-files.sh`
- Create: `src/korvid/__init__.py`, `src/korvid/py.typed`, and empty `__init__.py` in `src/korvid/{ui,core,agent,k8s,providers}/`
- Generate + commit: `uv.lock` (via `uv sync` in Step 9 — CI uses `uv sync --locked`, so the lockfile MUST be checked in)
- Test: `tests/test_sanity.py`

**Interfaces:**
- Produces: the gate stack every later task runs inside; `korvid.__version__ = "0.1.0"`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "korvid"
version = "0.1.0"
description = "AI-native Kubernetes TUI"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.11"
dependencies = [
    "textual>=8.0",
    "kubernetes_asyncio>=32.0",
    "pyyaml>=6.0",
]

[project.scripts]
korvid = "korvid.__main__:main"

[build-system]
# hatchling >=1.26 required: PEP 639 SPDX string form of `license` above
requires = ["hatchling>=1.26"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/korvid"]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5",
    "pytest-xdist>=3.6",
    "pytest-randomly>=3.15",
    "mypy>=1.14",
    "ruff>=0.8",
    "tach>=0.23",
    "deptry>=0.20",
    "pre-commit>=4",
    "types-PyYAML",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "SIM", "UP", "PT", "C90", "TID", "RUF", "S101"]
ignore = ["E501"]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.flake8-pytest-style]
mark-parentheses = false
fixture-parentheses = false

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["S101"]

[tool.mypy]
strict = true
warn_unused_ignores = true
mypy_path = "src"
packages = ["korvid"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "--strict-markers --tb=short"
testpaths = ["tests"]
filterwarnings = ["error"]

[tool.coverage.run]
branch = true
source = ["src/korvid"]

[tool.coverage.report]
fail_under = 80
show_missing = true

[tool.typos.files]
extend-exclude = ["*.lock", "*.snap"]

[tool.deptry]
known_first_party = ["korvid"]
```

- [ ] **Step 2: Create tach.toml**

```toml
source_roots = ["src"]

[[modules]]
path = "korvid.ui"
depends_on = ["korvid.core", "korvid.agent", "korvid.k8s"]

[[modules]]
path = "korvid.core"
depends_on = ["korvid.k8s"]

[[modules]]
path = "korvid.agent"
depends_on = ["korvid.core", "korvid.k8s"]

[[modules]]
path = "korvid.k8s"
depends_on = []

[[modules]]
path = "korvid.providers"
depends_on = ["korvid.agent"]
```

- [ ] **Step 3: Create .pre-commit-config.yaml**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/crate-ci/typos
    rev: v1.28.4
    hooks:
      - id: typos
        pass_filenames: false
  - repo: https://github.com/abravalheri/validate-pyproject
    rev: v0.23
    hooks:
      - id: validate-pyproject
  - repo: local
    hooks:
      - id: mypy
        name: mypy
        entry: uv run mypy
        language: system
        types: [python]
        pass_filenames: false
      - id: no-bare-type-ignore
        name: No bare 'type: ignore'
        language: pygrep
        entry: '# type: ignore(?!\[)'
        types: [python]
```

(Note: pin `rev` values to the latest tags available at execution time — `pre-commit autoupdate` after creating the file.)

- [ ] **Step 4: Create Makefile**

```makefile
.PHONY: lint format typecheck test check

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/

typecheck:
	uv run mypy

test:
	uv run pytest -x -q

check: lint typecheck test
	uv run tach check
```

- [ ] **Step 5: Create CI workflow `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9  # v9.0.0
        with:
          python-version: ${{ matrix.python-version }}
          enable-cache: true
      - run: uv sync --locked --dev
      - run: uv run ruff check src/ tests/
      - run: uv run ruff format --check src/ tests/
      - run: uv run mypy
      - run: uv run tach check
      - run: uvx deptry src/
      - run: uv run pytest --cov --cov-fail-under=80

  ty-experimental:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9  # v9.0.0
      - run: uvx ty check src/ || true
```

- [ ] **Step 6: Create .claude/settings.json and hook script**

`.claude/settings.json`:
```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "file=$(jq -r '.tool_input.file_path // empty'); if echo \"$file\" | grep -q '\\.py$'; then uv run ruff check --fix --quiet \"$file\" 2>/dev/null; uv run ruff format --quiet \"$file\" 2>/dev/null; fi; exit 0"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/protect-files.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run pytest -x -q --tb=short 2>&1 | tail -20"
          }
        ]
      }
    ]
  }
}
```

`.claude/hooks/protect-files.sh` (make executable, `chmod +x`):
```bash
#!/bin/bash
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
PROTECTED=("uv.lock" ".github/workflows/" "tach.toml" ".pre-commit-config.yaml")
for pattern in "${PROTECTED[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]]; then
    echo "Blocked: $FILE_PATH is a protected gate file — ask the human to change it" >&2
    exit 2
  fi
done
exit 0
```

- [ ] **Step 7: Create package skeleton**

`src/korvid/__init__.py`:
```python
"""korvid — AI-native Kubernetes TUI."""

__version__ = "0.1.0"
```

Create empty `src/korvid/py.typed` and empty `__init__.py` files:
```bash
touch src/korvid/py.typed
mkdir -p src/korvid/ui src/korvid/core src/korvid/agent src/korvid/k8s src/korvid/providers tests
for d in ui core agent k8s providers; do touch "src/korvid/$d/__init__.py"; done
```

- [ ] **Step 8: Write the sanity test**

`tests/test_sanity.py`:
```python
"""Gate-stack sanity: the package imports and has a version."""

import korvid


def test_version() -> None:
    assert korvid.__version__ == "0.1.0"
```

- [ ] **Step 9: Install and run all gates**

```bash
uv sync --dev                 # also generates uv.lock — commit it (CI runs `uv sync --locked`)
uv run pre-commit autoupdate
uv run pytest -x -q          # expect: 1 passed
make check                    # expect: all green
uv run pre-commit install
```

- [ ] **Step 10: Commit (verify `uv.lock` is included)**

```bash
git add -A
git status --short | grep uv.lock   # must show uv.lock staged
git commit -m "feat: repository gates scaffold (pyproject, tach, pre-commit, CI, claude hooks)"
```

---

### Task 2: Config loader

**Files:**
- Create: `src/korvid/core/config.py`
- Test: `tests/core/test_config.py` (+ empty `tests/core/__init__.py`, `tests/__init__.py`)

**Interfaces:**
- Produces: `KorvidConfig` frozen dataclass with fields `kube_context: str | None`, `namespace: str | None`, `agent_enabled: bool`, `agent_provider: str | None`, `keybindings: dict[str, str]`; `load_config(path: Path | None = None) -> KorvidConfig`. Zero-config: returns defaults when file absent. Agent activates only when a provider is configured (design doc §6.3).

- [ ] **Step 1: Write failing tests**

`tests/core/test_config.py`:
```python
from pathlib import Path

from korvid.core.config import KorvidConfig, load_config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg == KorvidConfig()
    assert cfg.agent_enabled is False  # no provider -> agent off


def test_load_from_yaml(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(
        "kube_context: prod\n"
        "namespace: default\n"
        "agent:\n  provider: anthropic\n"
        "keybindings:\n  quit: q\n"
    )
    cfg = load_config(f)
    assert cfg.kube_context == "prod"
    assert cfg.namespace == "default"
    assert cfg.agent_provider == "anthropic"
    assert cfg.agent_enabled is True  # provider present -> auto-enabled
    assert cfg.keybindings == {"quit": "q"}


def test_explicit_agent_off_wins(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("agent:\n  provider: anthropic\n  enabled: false\n")
    cfg = load_config(f)
    assert cfg.agent_enabled is False  # explicit off switch (design doc §6.3-4)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.core.config'`

- [ ] **Step 3: Implement**

`src/korvid/core/config.py`:
```python
"""Single-file configuration (design doc §5-7): ~/.config/korvid/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "korvid" / "config.yaml"


@dataclass(frozen=True)
class KorvidConfig:
    kube_context: str | None = None
    namespace: str | None = None
    agent_enabled: bool = False
    agent_provider: str | None = None
    keybindings: dict[str, str] = field(default_factory=dict)


def load_config(path: Path | None = None) -> KorvidConfig:
    """Load config; missing file means zero-config defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        return KorvidConfig()
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text()) or {}
    agent_raw: dict[str, Any] = raw.get("agent") or {}
    provider: str | None = agent_raw.get("provider")
    # Auto-activation: provider present -> on, unless explicitly disabled (§6.3).
    enabled = bool(provider) and agent_raw.get("enabled", True) is not False
    return KorvidConfig(
        kube_context=raw.get("kube_context"),
        namespace=raw.get("namespace"),
        agent_enabled=enabled,
        agent_provider=provider,
        keybindings=dict(raw.get("keybindings") or {}),
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: 3 passed

- [ ] **Step 5: Gates + commit**

```bash
make check
git add src/korvid/core/config.py tests/
git commit -m "feat: single-file config loader with agent auto-activation"
```

---

### Task 3: k8s client wrapper

**Files:**
- Create: `src/korvid/k8s/client.py`, `src/korvid/k8s/models.py`
- Test: `tests/k8s/test_client.py`, `tests/k8s/test_models.py` (+ `tests/k8s/__init__.py`)

**Interfaces:**
- Produces:
  - `PodSummary` frozen dataclass: `name: str`, `namespace: str`, `phase: str`, `ready: str` (e.g. `"1/2"`), `restarts: int`, `node: str | None`
  - `PodSummary.from_manifest(obj: dict[str, Any]) -> PodSummary` — parses a pod manifest dict
  - `KubeClient` with `async connect(context: str | None) -> None`, `async list_namespaces() -> list[str]`, `async list_pods(namespace: str) -> list[PodSummary]`, `async watch_pods(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]` (yields `("ADDED"|"MODIFIED"|"DELETED", pod)`), `async close() -> None`
- Design: `KubeClient` holds a `kubernetes_asyncio` `ApiClient`; all parsing lives in `models.py` so it is testable without any cluster or mock.

- [ ] **Step 1: Write failing model tests**

`tests/k8s/test_models.py`:
```python
from typing import Any

from korvid.k8s.models import PodSummary

POD: dict[str, Any] = {
    "metadata": {"name": "checkout-7d9f", "namespace": "prod"},
    "spec": {"nodeName": "node-1"},
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {"ready": True, "restartCount": 0},
            {"ready": False, "restartCount": 7},
        ],
    },
}


def test_from_manifest() -> None:
    pod = PodSummary.from_manifest(POD)
    assert pod.name == "checkout-7d9f"
    assert pod.namespace == "prod"
    assert pod.phase == "Running"
    assert pod.ready == "1/2"
    assert pod.restarts == 7
    assert pod.node == "node-1"


def test_from_manifest_no_statuses() -> None:
    pod = PodSummary.from_manifest(
        {"metadata": {"name": "p", "namespace": "d"}, "spec": {}, "status": {"phase": "Pending"}}
    )
    assert pod.ready == "0/0"
    assert pod.restarts == 0
    assert pod.node is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/k8s/test_models.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement models**

`src/korvid/k8s/models.py`:
```python
"""Typed summaries of Kubernetes objects (parsing isolated from I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PodSummary:
    name: str
    namespace: str
    phase: str
    ready: str
    restarts: int
    node: str | None

    @classmethod
    def from_manifest(cls, obj: dict[str, Any]) -> PodSummary:
        meta = obj.get("metadata") or {}
        status = obj.get("status") or {}
        statuses: list[dict[str, Any]] = status.get("containerStatuses") or []
        ready_count = sum(1 for s in statuses if s.get("ready"))
        # kubectl's RESTARTS column is the SUM across containers — match that expectation.
        restarts = sum(int(s.get("restartCount", 0)) for s in statuses)
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            phase=str(status.get("phase", "Unknown")),
            ready=f"{ready_count}/{len(statuses)}",
            restarts=restarts,
            node=(obj.get("spec") or {}).get("nodeName"),
        )
```

- [ ] **Step 4: Run model tests to verify pass**

Run: `uv run pytest tests/k8s/test_models.py -v`
Expected: 2 passed

- [ ] **Step 5: Write failing client tests (mocked API)**

`tests/k8s/test_client.py`:
```python
from typing import Any
from unittest.mock import AsyncMock, patch

from korvid.k8s.client import KubeClient


def _pod(name: str, ns: str = "default") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {},
        "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
    }


async def test_list_pods_parses_summaries() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {"items": [_pod("a"), _pod("b")]}
    with patch.object(client, "_core_v1", fake_v1):
        pods = await client.list_pods("default")
    assert [p.name for p in pods] == ["a", "b"]
    fake_v1.list_namespaced_pod.assert_awaited_once_with(
        "default", _preload_content=False
    )
```

(Adjust the mock surface in Step 6 if the real call shape differs — the test pins the contract: `list_pods` returns parsed `PodSummary` objects in API order.)

- [ ] **Step 6: Implement client**

`src/korvid/k8s/client.py`:
```python
"""Async Kubernetes client wrapper. The only module that talks to the API server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio import watch as k8s_watch

from korvid.k8s.models import PodSummary


class KubeClient:
    """Thin wrapper over kubernetes_asyncio; returns typed summaries."""

    def __init__(self) -> None:
        self._api: k8s_client.ApiClient | None = None
        self._core_v1: k8s_client.CoreV1Api | None = None

    async def connect(self, context: str | None = None) -> None:
        await k8s_config.load_kube_config(context=context)
        self._api = k8s_client.ApiClient()
        self._core_v1 = k8s_client.CoreV1Api(self._api)

    async def list_namespaces(self) -> list[str]:
        assert self._core_v1 is not None, "connect() first"
        resp = await self._core_v1.list_namespace(_preload_content=False)
        data = await _to_dict(resp)
        return [item["metadata"]["name"] for item in data.get("items", [])]

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        assert self._core_v1 is not None, "connect() first"
        resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
        data = await _to_dict(resp)
        return [PodSummary.from_manifest(item) for item in data.get("items", [])]

    async def watch_pods(self, namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        assert self._core_v1 is not None, "connect() first"
        w = k8s_watch.Watch()
        async with w.stream(self._core_v1.list_namespaced_pod, namespace) as stream:
            async for event in stream:
                yield (
                    str(event["type"]),
                    PodSummary.from_manifest(event["raw_object"]),
                )

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()


async def _to_dict(resp: Any) -> dict[str, Any]:
    """Normalize aiohttp response or dict into a plain dict."""
    if isinstance(resp, dict):
        return resp
    body = await resp.read()
    result: dict[str, Any] = json.loads(body)
    return result
```

- [ ] **Step 7: Run all k8s tests**

Run: `uv run pytest tests/k8s/ -v`
Expected: all pass (fix the mock/call-shape mismatch on the client test if needed — keep the contract, adapt the internals)

- [ ] **Step 8: Gates + commit**

```bash
make check
git add src/korvid/k8s/ tests/k8s/
git commit -m "feat: async k8s client wrapper with typed pod summaries"
```

---

### Task 4: ResourceStore

**Files:**
- Create: `src/korvid/core/store.py`
- Test: `tests/core/test_store.py`

**Interfaces:**
- Consumes: `PodSummary` from Task 3
- Produces: `ResourceStore` with `apply_event(kind: str, event_type: str, obj: PodSummary) -> None`, `get(kind: str, namespace: str) -> list[PodSummary]` (sorted by name), `subscribe(callback: Callable[[str], None]) -> None` (callback receives `kind` on any change). Later tasks (WatchManager, UI) rely on exactly these three methods.

- [ ] **Step 1: Write failing tests**

`tests/core/test_store.py`:
```python
from korvid.core.store import ResourceStore
from korvid.k8s.models import PodSummary


def _pod(name: str, ns: str = "default") -> PodSummary:
    return PodSummary(name=name, namespace=ns, phase="Running", ready="1/1", restarts=0, node=None)


def test_apply_added_and_get_sorted() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("b"))
    store.apply_event("pods", "ADDED", _pod("a"))
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]


def test_modified_replaces() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a"))
    updated = PodSummary(name="a", namespace="default", phase="Failed", ready="0/1", restarts=3, node=None)
    store.apply_event("pods", "MODIFIED", updated)
    (pod,) = store.get("pods", "default")
    assert pod.phase == "Failed"


def test_deleted_removes() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a"))
    store.apply_event("pods", "DELETED", _pod("a"))
    assert store.get("pods", "default") == []


def test_subscriber_notified_with_kind() -> None:
    store = ResourceStore()
    seen: list[str] = []
    store.subscribe(seen.append)
    store.apply_event("pods", "ADDED", _pod("a"))
    assert seen == ["pods"]


def test_broken_subscriber_does_not_block_others() -> None:
    store = ResourceStore()
    seen: list[str] = []

    def broken(kind: str) -> None:
        raise RuntimeError("subscriber bug")

    store.subscribe(broken)
    store.subscribe(seen.append)
    store.apply_event("pods", "ADDED", _pod("a"))  # must not raise
    assert seen == ["pods"]


def test_namespaces_isolated() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a", ns="prod"))
    assert store.get("pods", "default") == []
    assert [p.name for p in store.get("pods", "prod")] == ["a"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_store.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

`src/korvid/core/store.py`:
```python
"""In-memory resource cache fed by watch events; the UI's single read model.

Subscriber callbacks are isolated: a buggy subscriber must never propagate
into the watch loop that calls apply_event (it would kill the whole watch).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from korvid.k8s.models import PodSummary

logger = logging.getLogger(__name__)


class ResourceStore:
    def __init__(self) -> None:
        # {(kind, namespace): {name: obj}}
        self._data: dict[tuple[str, str], dict[str, PodSummary]] = {}
        self._subscribers: list[Callable[[str], None]] = []

    def apply_event(self, kind: str, event_type: str, obj: PodSummary) -> None:
        bucket = self._data.setdefault((kind, obj.namespace), {})
        if event_type == "DELETED":
            bucket.pop(obj.name, None)
        else:  # ADDED / MODIFIED
            bucket[obj.name] = obj
        for callback in self._subscribers:
            try:
                callback(kind)
            except Exception:  # noqa: BLE001 - subscriber bugs must not kill the watch loop
                logger.exception("resource store subscriber failed")

    def get(self, kind: str, namespace: str) -> list[PodSummary]:
        bucket = self._data.get((kind, namespace), {})
        return sorted(bucket.values(), key=lambda o: o.name)

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._subscribers.append(callback)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_store.py -v`
Expected: 6 passed

- [ ] **Step 5: Gates + commit**

```bash
make check
git add src/korvid/core/store.py tests/core/test_store.py
git commit -m "feat: resource store with watch-event application and subscriptions"
```

---

### Task 5: WatchManager (selective watch)

**Files:**
- Create: `src/korvid/core/watch.py`
- Test: `tests/core/test_watch.py`

**Interfaces:**
- Consumes: `ResourceStore.apply_event`; a watch-source callable with the same shape as `KubeClient.watch_pods`
- Produces: `WatchManager(store: ResourceStore, watch_source: WatchSource, *, on_error: Callable[[str], None] | None = None, retry_delay: float = 1.0, max_retries: int = 5)` where `WatchSource = Callable[[str], AsyncIterator[tuple[str, PodSummary]]]`; methods `async start(kind: str, namespace: str) -> None` (idempotent), `async stop(kind: str, namespace: str) -> None`, `async stop_all() -> None`, property `active: set[tuple[str, str]]`. Selective-watch principle (design doc §5-6): only what's on screen gets a watch task.
- Resilience contract (k8s API servers periodically close watch streams): a stream that **ends normally reconnects forever**; a stream that **raises** retries up to `max_retries` consecutive failures with `retry_delay` between attempts, then gives up — the dead task is removed from `active` and `on_error` receives a human-readable message. Watch tasks must never die silently while still appearing active.

- [ ] **Step 1: Write failing tests**

`tests/core/test_watch.py`:
```python
import asyncio
from collections.abc import AsyncIterator

from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary


def _pod(name: str) -> PodSummary:
    return PodSummary(name=name, namespace="default", phase="Running", ready="1/1", restarts=0, node=None)


def make_source(events: list[tuple[str, PodSummary]], forever: bool = True):
    async def source(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        for ev in events:
            yield ev
        while forever:  # simulate an open stream
            await asyncio.sleep(0.01)
            if False:
                yield ("", _pod(""))  # pragma: no cover - typing aid

    return source


async def test_start_feeds_store() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([("ADDED", _pod("a")), ("ADDED", _pod("b"))]))
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]
    assert mgr.active == {("pods", "default")}
    await mgr.stop_all()


async def test_start_is_idempotent() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([]))
    await mgr.start("pods", "default")
    await mgr.start("pods", "default")
    assert len(mgr.active) == 1
    await mgr.stop_all()


async def test_stop_cancels() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([]))
    await mgr.start("pods", "default")
    await mgr.stop("pods", "default")
    assert mgr.active == set()


async def test_stream_end_reconnects() -> None:
    store = ResourceStore()
    calls = 0

    async def flaky(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        nonlocal calls
        calls += 1
        yield ("ADDED", _pod(f"p{calls}"))
        # stream ends -> k8s watch timeout simulation; manager must reconnect

    mgr = WatchManager(store, flaky, retry_delay=0)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert calls >= 2
    await mgr.stop_all()


async def test_failing_watch_reports_and_removes_task() -> None:
    store = ResourceStore()
    errors: list[str] = []

    async def broken(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        raise RuntimeError("boom")
        yield ("", _pod(""))  # pragma: no cover - makes this an async generator

    mgr = WatchManager(store, broken, on_error=errors.append, retry_delay=0, max_retries=2)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert mgr.active == set()  # dead task removed, not lying around as "active"
    assert errors
    assert "boom" in errors[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_watch.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

`src/korvid/core/watch.py`:
```python
"""Selective watch: one task per (kind, namespace) actually on screen (§5-6).

Streams that end normally (k8s API servers close watches periodically)
reconnect forever. Streams that raise retry up to max_retries consecutive
failures, then the task is removed from `active` and on_error is notified —
watch tasks never die silently.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

from korvid.core.store import ResourceStore
from korvid.k8s.models import PodSummary

WatchSource = Callable[[str], AsyncIterator[tuple[str, PodSummary]]]


class WatchManager:
    def __init__(
        self,
        store: ResourceStore,
        watch_source: WatchSource,
        *,
        on_error: Callable[[str], None] | None = None,
        retry_delay: float = 1.0,
        max_retries: int = 5,
    ) -> None:
        self._store = store
        self._source = watch_source
        self.on_error = on_error  # public: the UI wires this after construction
        self._retry_delay = retry_delay
        self._max_retries = max_retries
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    @property
    def active(self) -> set[tuple[str, str]]:
        return set(self._tasks)

    async def start(self, kind: str, namespace: str) -> None:
        key = (kind, namespace)
        if key in self._tasks:
            return
        self._tasks[key] = asyncio.create_task(self._run(kind, namespace))

    async def stop(self, kind: str, namespace: str) -> None:
        task = self._tasks.pop((kind, namespace), None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop_all(self) -> None:
        for kind, namespace in list(self._tasks):
            await self.stop(kind, namespace)

    async def _run(self, kind: str, namespace: str) -> None:
        failures = 0
        while True:
            try:
                async for event_type, obj in self._source(namespace):
                    failures = 0
                    self._store.apply_event(kind, event_type, obj)
                # Stream ended normally (server-side watch timeout) -> reconnect.
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report + retry, never die silently
                failures += 1
                if failures >= self._max_retries:
                    if self.on_error is not None:
                        self.on_error(f"watch {kind}/{namespace} failed: {exc}")
                    break
            await asyncio.sleep(self._retry_delay)
        self._tasks.pop((kind, namespace), None)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_watch.py -v`
Expected: 5 passed

- [ ] **Step 5: Gates + commit**

```bash
make check
git add src/korvid/core/watch.py tests/core/test_watch.py
git commit -m "feat: selective watch manager with reconnect and failure reporting"
```

---

### Task 6: UI Bus messages + error mapping

**Files:**
- Create: `src/korvid/ui/messages.py`, `src/korvid/core/errors.py`
- Test: `tests/core/test_errors.py`, `tests/ui/test_messages.py` (+ `tests/ui/__init__.py`)

**Interfaces:**
- Produces (messages — the UI Bus vocabulary, harlequin pipeline pattern):
  - `NavigateCommand(view: str, namespace: str | None = None)` — e.g. `view="pods"`
  - `FilterCommand(pattern: str)` / `ClearFilter()`
  - `ResourcesUpdated(kind: str)`
  - `ShowError(title: str, detail: str)`
- Produces (errors): `explain_api_error(status: int, reason: str, resource: str, namespace: str | None) -> str` — human-readable RBAC/auth mapping (design doc §5-5)

- [ ] **Step 1: Write failing error tests**

`tests/core/test_errors.py`:
```python
from korvid.core.errors import explain_api_error


def test_403_names_the_permission_and_namespace() -> None:
    msg = explain_api_error(403, "Forbidden", "pods", "prod")
    assert "pods" in msg
    assert "prod" in msg
    assert "permission" in msg.lower()


def test_401_suggests_reauth() -> None:
    msg = explain_api_error(401, "Unauthorized", "pods", None)
    assert "credential" in msg.lower() or "re-auth" in msg.lower()


def test_unknown_falls_back_to_reason() -> None:
    msg = explain_api_error(500, "Internal error", "pods", None)
    assert "Internal error" in msg
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_errors.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement errors + messages**

`src/korvid/core/errors.py`:
```python
"""Map raw API errors to actionable, human-readable messages (§5-5)."""

from __future__ import annotations


def explain_api_error(status: int, reason: str, resource: str, namespace: str | None) -> str:
    ns_part = f" (namespace: {namespace})" if namespace else ""
    if status == 403:
        return f"No permission to access {resource}{ns_part}. Check your RBAC role bindings."
    if status == 401:
        return "Credentials expired or invalid — re-authenticate with your cluster (e.g. renew token)."
    return f"API error {status} on {resource}{ns_part}: {reason}"
```

`src/korvid/ui/messages.py`:
```python
"""UI Bus vocabulary: user keystrokes and agent UI-control emit the same Messages."""

from __future__ import annotations

from textual.message import Message


class NavigateCommand(Message):
    def __init__(self, view: str, namespace: str | None = None) -> None:
        self.view = view
        self.namespace = namespace
        super().__init__()


class FilterCommand(Message):
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        super().__init__()


class ClearFilter(Message):
    pass


class ResourcesUpdated(Message):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__()


class ShowError(Message):
    def __init__(self, title: str, detail: str) -> None:
        self.title = title
        self.detail = detail
        super().__init__()
```

`tests/ui/test_messages.py`:
```python
from korvid.ui.messages import FilterCommand, NavigateCommand, ResourcesUpdated


def test_navigate_carries_view_and_namespace() -> None:
    msg = NavigateCommand("pods", namespace="prod")
    assert msg.view == "pods"
    assert msg.namespace == "prod"


def test_filter_carries_pattern() -> None:
    assert FilterCommand("check").pattern == "check"


def test_resources_updated_carries_kind() -> None:
    assert ResourcesUpdated("pods").kind == "pods"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_errors.py tests/ui/test_messages.py -v`
Expected: 6 passed

- [ ] **Step 5: Gates + commit**

```bash
make check
git add src/korvid/core/errors.py src/korvid/ui/messages.py tests/
git commit -m "feat: UI bus message vocabulary and RBAC error mapping"
```

---

### Task 7: App shell + pods table vertical slice

**Files:**
- Create: `src/korvid/ui/app.py`, `src/korvid/ui/widgets/__init__.py`, `src/korvid/ui/widgets/resource_table.py`, `src/korvid/__main__.py`
- Test: `tests/ui/test_app.py`

**Interfaces:**
- Consumes: `ResourceStore`, `WatchManager`, `KorvidConfig`, messages from Task 6
- Produces:
  - `ResourceTable(DataTable)` widget with `update_rows(pods: list[PodSummary], pattern: str = "") -> None`
  - `KorvidApp(App)` constructed as `KorvidApp(config: KorvidConfig, store: ResourceStore, watch_manager: WatchManager)`; on mount starts the pods watch for the current namespace and renders the table; handles `ResourcesUpdated` by refreshing rows
  - `main() -> None` in `__main__.py` — the composition root (real `KubeClient` wired here and only here)
- Testing: Pilot with a fake watch source — **no real cluster in any test**.

- [ ] **Step 1: Write failing Pilot test**

`tests/ui/test_app.py`:
```python
import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.resource_table import ResourceTable


def _pod(name: str, phase: str = "Running") -> PodSummary:
    return PodSummary(name=name, namespace="default", phase=phase, ready="1/1", restarts=0, node=None)


def fake_source(pods: list[PodSummary]):
    async def source(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


def make_app(pods: list[PodSummary]) -> KorvidApp:
    store = ResourceStore()
    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, fake_source(pods)),
    )


async def test_pods_appear_in_table() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2", phase="CrashLoopBackOff")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        assert table.get_row_at(0)[0] == "api-1"


async def test_watch_update_refreshes_table() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.store.apply_event("pods", "ADDED", _pod("zzz-new"))
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement widget, app, entry point**

`src/korvid/ui/widgets/resource_table.py`:
```python
"""Pod list table — the first resource view."""

from __future__ import annotations

from textual.widgets import DataTable

from korvid.k8s.models import PodSummary

COLUMNS = ("NAME", "READY", "STATUS", "RESTARTS", "NODE")


class ResourceTable(DataTable[str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns(*COLUMNS)

    def update_rows(self, pods: list[PodSummary], pattern: str = "") -> None:
        self.clear()
        for pod in pods:
            if pattern and pattern.lower() not in pod.name.lower():
                continue
            self.add_row(pod.name, pod.ready, pod.phase, str(pod.restarts), pod.node or "-", key=pod.name)
```

`src/korvid/ui/app.py`:
```python
"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.ui.messages import ResourcesUpdated, ShowError
from korvid.ui.widgets.resource_table import ResourceTable


class KorvidApp(App[None]):
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(
        self,
        config: KorvidConfig,
        store: ResourceStore,
        watch_manager: WatchManager,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self.current_namespace = config.namespace or "default"
        self.filter_pattern = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield ResourceTable()
        yield Footer()

    async def on_mount(self) -> None:
        # Both callbacks fire from watch tasks on the same loop; post_message is
        # loop-safe. Watch tasks are cancelled in on_unmount before shutdown to
        # avoid posting to a closing app.
        self.store.subscribe(lambda kind: self.post_message(ResourcesUpdated(kind)))
        self.watch_manager.on_error = lambda detail: self.post_message(
            ShowError("Watch failed", detail)
        )
        await self.watch_manager.start("pods", self.current_namespace)

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        table = self.query_one(ResourceTable)
        table.update_rows(
            self.store.get(message.kind, self.current_namespace), self.filter_pattern
        )

    def on_show_error(self, message: ShowError) -> None:
        self.notify(message.detail, title=message.title, severity="error")

    async def on_unmount(self) -> None:
        await self.watch_manager.stop_all()
```

`src/korvid/__main__.py`:
```python
"""Composition root — the only place real dependencies are wired together.

Everything (connect, app, close) runs inside ONE event loop via run_async:
kubernetes_asyncio's ApiClient binds its aiohttp session to the loop it was
created on, so separate asyncio.run() calls would break with
"Event loop is closed" / "attached to a different loop".
"""

from __future__ import annotations

import asyncio

from korvid.core.config import load_config
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.ui.app import KorvidApp


async def _run() -> None:
    config = load_config()
    kube = KubeClient()
    await kube.connect(config.kube_context)
    store = ResourceStore()
    watch_manager = WatchManager(store, kube.watch_pods)
    app = KorvidApp(config=config, store=store, watch_manager=watch_manager)
    try:
        await app.run_async()
    finally:
        await kube.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: 2 passed

- [ ] **Step 5: Full gates + commit**

```bash
make check
git add src/korvid/ui/ src/korvid/__main__.py tests/ui/
git commit -m "feat: app shell with pods table vertical slice (store -> bus -> table)"
```

---

### Task 8: `:` command bar

**Files:**
- Create: `src/korvid/ui/command.py` (parser — pure logic), `src/korvid/ui/widgets/command_bar.py`
- Modify: `src/korvid/ui/app.py` (mount bar, handle commands)
- Test: `tests/ui/test_command.py`, extend `tests/ui/test_app.py`

**Interfaces:**
- Produces: `parse_command(text: str) -> NavigateCommand | FilterCommand | QuitCommand | UnknownCommand` where `QuitCommand`/`UnknownCommand(text: str)` are new Messages in `ui/messages.py`. Grammar (design doc §5): `pods` → navigate; `ns <name>` → namespace switch (NavigateCommand with current view); `q`/`quit` → quit; anything else → UnknownCommand.
- `CommandBar(Input)` widget: hidden by default; `:` key shows and focuses it; Enter parses and posts the message; Esc hides.

- [ ] **Step 1: Write failing parser tests**

`tests/ui/test_command.py`:
```python
from korvid.ui.command import parse_command
from korvid.ui.messages import NavigateCommand, QuitCommand, UnknownCommand


def test_pods_navigates() -> None:
    msg = parse_command("pods")
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "pods"


def test_ns_switches_namespace() -> None:
    msg = parse_command("ns prod")
    assert isinstance(msg, NavigateCommand)
    assert msg.namespace == "prod"


def test_quit() -> None:
    assert isinstance(parse_command("q"), QuitCommand)
    assert isinstance(parse_command("quit"), QuitCommand)


def test_unknown_preserved() -> None:
    msg = parse_command("frobnicate all")
    assert isinstance(msg, UnknownCommand)
    assert msg.text == "frobnicate all"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ui/test_command.py -v`
Expected: FAIL

- [ ] **Step 3: Implement parser + messages + widget + app wiring**

Add to `src/korvid/ui/messages.py`:
```python
class QuitCommand(Message):
    pass


class UnknownCommand(Message):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()
```

`src/korvid/ui/command.py`:
```python
"""`:` command grammar — familiar TUI conventions. UnknownCommand is the future agent fallthrough hook."""

from __future__ import annotations

from korvid.ui.messages import NavigateCommand, QuitCommand, UnknownCommand

_VIEWS = {"pods"}


def parse_command(text: str) -> NavigateCommand | QuitCommand | UnknownCommand:
    parts = text.strip().split()
    if not parts:
        return UnknownCommand(text)
    head, *rest = parts
    if head in {"q", "quit"}:
        return QuitCommand()
    if head in _VIEWS and not rest:
        return NavigateCommand(head)
    if head == "ns" and len(rest) == 1:
        return NavigateCommand("pods", namespace=rest[0])
    return UnknownCommand(text)
```

`src/korvid/ui/widgets/command_bar.py`:
```python
from __future__ import annotations

from textual.widgets import Input

from korvid.ui.command import parse_command


class CommandBar(Input):
    """Hidden `:` command input; Enter dispatches onto the UI Bus."""

    def on_mount(self) -> None:
        self.display = False

    def open(self) -> None:
        self.value = ""
        self.display = True
        self.focus()

    def dismiss_bar(self) -> None:
        self.display = False
        self.value = ""

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.post_message(parse_command(event.value))
        self.dismiss_bar()

    async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]  # Textual event union
        if event.key == "escape":
            self.dismiss_bar()
```

Modify `src/korvid/ui/app.py` — add to imports, `compose`, bindings and handlers:
```python
# imports
from korvid.ui.messages import NavigateCommand, QuitCommand, ResourcesUpdated, UnknownCommand
from korvid.ui.widgets.command_bar import CommandBar

# BINDINGS — add:
#   ("colon", "open_command", "Command")

# compose() — add before Footer:
#   yield CommandBar()

# new methods on KorvidApp:
    def action_open_command(self) -> None:
        self.query_one(CommandBar).open()

    async def on_navigate_command(self, message: NavigateCommand) -> None:
        if message.namespace and message.namespace != self.current_namespace:
            await self.watch_manager.stop("pods", self.current_namespace)
            self.current_namespace = message.namespace
            await self.watch_manager.start("pods", self.current_namespace)
        self.post_message(ResourcesUpdated("pods"))

    def on_quit_command(self, message: QuitCommand) -> None:
        self.exit()

    def on_unknown_command(self, message: UnknownCommand) -> None:
        self.notify(f"Unknown command: {message.text}", severity="warning")
```

- [ ] **Step 4: Add Pilot integration test**

Append to `tests/ui/test_app.py`:
```python
async def test_colon_opens_command_bar_and_ns_switch() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "ns prod":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.current_namespace == "prod"
```

- [ ] **Step 5: Run all UI tests**

Run: `uv run pytest tests/ui/ -v`
Expected: all pass

- [ ] **Step 6: Gates + commit**

```bash
make check
git add src/korvid/ui/ tests/ui/
git commit -m "feat: colon command bar with familiar TUI command grammar"
```

---

### Task 9: `/` filter

**Files:**
- Create: `src/korvid/ui/widgets/filter_bar.py`
- Modify: `src/korvid/ui/app.py`
- Test: extend `tests/ui/test_app.py`

**Interfaces:**
- Consumes: `FilterCommand` / `ClearFilter` messages (Task 6), `ResourceTable.update_rows(pods, pattern)` (Task 7)
- Produces: `FilterBar(Input)` — `/` opens it; typing posts `FilterCommand(value)` live on change; Esc posts `ClearFilter` and hides. App stores `filter_pattern` and re-renders.

- [ ] **Step 1: Write failing Pilot test**

Append to `tests/ui/test_app.py`:
```python
async def test_slash_filter_narrows_rows() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        for ch in "check":
            await pilot.press(ch)
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 1
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert table.row_count == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ui/test_app.py::test_slash_filter_narrows_rows -v`
Expected: FAIL

- [ ] **Step 3: Implement**

`src/korvid/ui/widgets/filter_bar.py`:
```python
from __future__ import annotations

from textual.widgets import Input

from korvid.ui.messages import ClearFilter, FilterCommand


class FilterBar(Input):
    """`/` live filter; Esc clears."""

    def on_mount(self) -> None:
        self.display = False

    def open(self) -> None:
        self.value = ""
        self.display = True
        self.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.post_message(FilterCommand(event.value))

    async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]  # Textual event union
        if event.key == "escape":
            self.display = False
            self.value = ""
            self.post_message(ClearFilter())
```

Modify `src/korvid/ui/app.py`:
```python
# imports: add ClearFilter, FilterCommand and FilterBar
# BINDINGS — add: ("slash", "open_filter", "Filter")
# compose() — add: yield FilterBar()

    def action_open_filter(self) -> None:
        self.query_one(FilterBar).open()

    def on_filter_command(self, message: FilterCommand) -> None:
        self.filter_pattern = message.pattern
        self.post_message(ResourcesUpdated("pods"))

    def on_clear_filter(self, message: ClearFilter) -> None:
        self.filter_pattern = ""
        self.post_message(ResourcesUpdated("pods"))
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -x -q`
Expected: all pass

- [ ] **Step 5: Gates + commit**

```bash
make check
git add src/korvid/ui/ tests/ui/
git commit -m "feat: slash live filter on the pods table"
```

---

### Task 10: Status bar + agent placeholder + release checks

**Files:**
- Create: `src/korvid/ui/widgets/status_bar.py`, `src/korvid/agent/provider.py`
- Modify: `src/korvid/ui/app.py`
- Test: `tests/agent/test_provider.py` (+ `tests/agent/__init__.py`), extend `tests/ui/test_app.py`

**Interfaces:**
- Produces: `StatusBar(Static)` showing `ctx:<name>  ns:<name>` and `⚡AI off` when no provider (design doc §6.3-3 discoverability comes later); `LLMProvider` ABC in `agent/provider.py` — the §3 boundary interface, implemented in later plans:

- [ ] **Step 1: Write failing tests**

`tests/agent/test_provider.py`:
```python
import pytest

from korvid.agent.provider import LLMProvider


def test_provider_is_abstract() -> None:
    with pytest.raises(TypeError, match="abstract"):
        LLMProvider()  # type: ignore[abstract]  # instantiating ABC is the test
```

Append to `tests/ui/test_app.py`:
```python
from korvid.ui.widgets.status_bar import StatusBar


async def test_status_bar_shows_ns_and_agent_state() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        bar = app.query_one(StatusBar)
        text = str(bar.renderable)
        assert "default" in text
        assert "AI off" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/agent/ tests/ui/test_app.py::test_status_bar_shows_ns_and_agent_state -v`
Expected: FAIL

- [ ] **Step 3: Implement**

`src/korvid/agent/provider.py`:
```python
"""LLMProvider ABC — the pluggable boundary (design doc §6.3, standards §3).

Concrete adapters live in korvid/providers/ and register via the
entry_points group "korvid.provider". No default provider is bundled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class LLMProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name shown in the status bar."""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield completion events (text deltas and tool calls)."""
```

`src/korvid/ui/widgets/status_bar.py`:
```python
from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    def update_status(self, context: str | None, namespace: str, agent_label: str) -> None:
        ctx = context or "(current)"
        self.update(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}")
```

Modify `src/korvid/ui/app.py`:
```python
# compose() — add before Footer: yield StatusBar()
# on_mount() — add:
#     self._refresh_status()
# on_navigate_command() — add after namespace switch: self._refresh_status()

    def _refresh_status(self) -> None:
        label = "AI on" if self.config.agent_enabled else "AI off"
        self.query_one(StatusBar).update_status(
            self.config.kube_context, self.current_namespace, label
        )
```

- [ ] **Step 4: Run everything**

Run: `uv run pytest --cov --cov-fail-under=80`
Expected: all pass, coverage ≥ 80%

- [ ] **Step 5: Full gates + commit**

```bash
make check
git add -A
git commit -m "feat: status bar with agent state and LLMProvider boundary ABC"
```

---

## Out of Scope (subsequent plans)

- **Plan 2 — Log viewer**: multi-pod merged streams, JSON detection + formatted↔raw toggle (`f`), previous-container logs (`p`), reconnect indicator, search n/N, 2-pane split
- **Plan 3 — Universal resources & pod actions**: generic resource views for any kind incl. CRDs (API discovery), all-namespaces scope, describe view (`d`), shell-in (`s`, exec -it)
- **Plan 4 — Agent runtime**: agentic loop, ToolRegistry, provider adapters, approval gate, audit log, token budget/gauge, agent panel
- **Plan 5 — Diagnostics**: kubectl debug (`D`), event intelligence, ownership tree

## Verification at the End

```bash
uv run pytest --cov --cov-fail-under=80   # full suite green
make check                                 # ruff + mypy + tach green
uv run korvid                              # manual: boots against current kubeconfig, shows pods
```
