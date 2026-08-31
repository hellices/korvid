# Private-Key Redaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mask recognized private-key fields and complete private-key PEM blocks before Kubernetes data leaves Korvid.

**Architecture:** Extend the existing pure `korvid.core.redaction` boundary so every structural and free-form consumer receives the same behavior and `RedactionRecord` evidence. Keep the change local to the redactor and its unit tests; do not add consumer-specific wiring or new dependencies.

**Tech Stack:** Python 3.11+, standard-library `re`, pytest, Ruff, mypy strict

## Global Constraints

- Recognize `privateKey`, `private_key`, `client-private-key`, and `client-key-data`.
- Preserve harmless `publicKey`, `publicKeyId`, `secretKeyRef`, and generic `key` fields.
- Mask complete `PRIVATE KEY`, `ENCRYPTED PRIVATE KEY`, `RSA PRIVATE KEY`, `EC PRIVATE KEY`, and `OPENSSH PRIVATE KEY` PEM blocks.
- Preserve certificates, public-key blocks, incomplete blocks, and mismatched header/footer pairs.
- Record deterministic evidence with existing `RedactionRecord` values.
- Preserve the existing boundary split: #331 protects structured MCP resource
  reads and provider-bound text; #330 will add producer-side MCP log/event
  redaction.
- Do not add generic entropy detection or unrelated credential heuristics.
- Keep `core/` free of Textual imports and third-party dependencies.

---

### Task 1: Classify private-key structural fields

**Files:**
- Modify: `src/korvid/core/redaction.py:34-50`
- Modify: `tests/core/test_redaction.py`
- Modify: `tests/tools/test_executor_security.py`
- Modify: `tests/mcp/test_server.py`

**Interfaces:**
- Consumes: `denotes_secret(value: str) -> bool`, `_mask_reason(key: str, item: Any, *, secret_sibling: bool) -> str | None`
- Produces: structural recognition for normalized names `privatekey` and `clientkeydata`; no new public API

- [ ] **Step 1: Write failing structural classification tests**

Append these tests near the existing credential-name tests:

```python
@pytest.mark.parametrize(
    "name",
    [
        "privateKey",
        "private_key",
        "client-private-key",
        "client-key-data",
        "clientKeyData",
    ],
)
def test_private_key_names_are_credentials(name: str) -> None:
    assert denotes_secret(name)


@pytest.mark.parametrize("name", ["publicKey", "publicKeyId", "secretKeyRef", "key", "keyData"])
def test_public_and_generic_key_names_are_not_credentials(name: str) -> None:
    assert not denotes_secret(name)


def test_private_key_fields_are_masked_with_deterministic_evidence() -> None:
    document = {
        "spec": {
            "privateKey": {"raw": "private-key-sentinel"},
            "client-key-data": "client-key-sentinel",
            "publicKeyId": "public-key-id",
        }
    }

    redacted, records = redact_document(document, path="doc")

    assert redacted["spec"] == {
        "privateKey": MASK_PLACEHOLDER,
        "client-key-data": MASK_PLACEHOLDER,
        "publicKeyId": "public-key-id",
    }
    assert records == [
        RedactionRecord(path="doc.spec.privateKey", reason="sensitive-key"),
        RedactionRecord(path='doc.spec["client-key-data"]', reason="sensitive-key"),
    ]
```

Add `RedactionRecord` to the redaction import in
`tests/tools/test_executor_security.py`, then add:

```python
async def test_get_resource_masks_private_key_fields_before_bounding() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "ConfigMap",
        "metadata": {"name": "client-config"},
        "data": {
            "privateKey": "private-key-sentinel",
            "publicKeyId": "public-key-id",
        },
    }

    outcome = await make_executor(kube).execute_recorded(
        "get_resource",
        {"kind": "pods", "name": "client-config", "namespace": "default"},
    )
    loaded = yaml.safe_load(outcome.text)

    assert loaded["data"] == {
        "privateKey": MASK_PLACEHOLDER,
        "publicKeyId": "public-key-id",
    }
    assert outcome.redactions == (
        RedactionRecord(path="manifest.data.privateKey", reason="sensitive-key"),
    )
```

Add this MCP path regression near
`test_mcp_results_are_redacted_like_the_agent_path`:

```python
async def test_mcp_resource_results_mask_private_key_fields() -> None:
    class ManifestKube:
        async def get_object(
            self,
            meta: Any,
            namespace: str | None,
            name: str,
        ) -> dict[str, Any]:
            return {
                "kind": "ConfigMap",
                "metadata": {"name": name},
                "data": {
                    "client-key-data": "mcp-private-key-sentinel",
                    "publicKeyId": "public-key-id",
                },
            }

    executor = ToolExecutor(ManifestKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource",
        {"kind": "pods", "name": "client-config", "namespace": "default"},
    )

    loaded = yaml.safe_load(content[0].text)
    assert loaded["data"] == {
        "client-key-data": MASK_PLACEHOLDER,
        "publicKeyId": "public-key-id",
    }
    assert "mcp-private-key-sentinel" not in content[0].text
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest \
  -p no:tach tests/core/test_redaction.py tests/tools/test_executor_security.py \
  tests/mcp/test_server.py \
  -k 'private_key_names or public_and_generic_key_names or private_key_fields or private_key_fields_before_bounding or mcp_resource_results_mask_private_key_fields' -q
```

Expected: the positive private-key cases fail because `denotes_secret` returns `False`; negative cases pass.

- [ ] **Step 3: Add the minimal normalized names**

Add these entries to `_SENSITIVE_NAMES` in `src/korvid/core/redaction.py`:

```python
        "privatekey",
        "clientkeydata",
```

Keep `_MAX_NAME_WINDOW = 3`; both new normalized compounds fit within the existing bound.

- [ ] **Step 4: Run targeted tests and Ruff**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest \
  -p no:tach tests/core/test_redaction.py tests/tools/test_executor_security.py \
  tests/mcp/test_server.py \
  -k 'private_key_names or public_and_generic_key_names or private_key_fields or private_key_fields_before_bounding or mcp_resource_results_mask_private_key_fields' -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/core/redaction.py tests/core/test_redaction.py \
  tests/tools/test_executor_security.py tests/mcp/test_server.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/core/redaction.py tests/core/test_redaction.py \
  tests/tools/test_executor_security.py tests/mcp/test_server.py
```

Expected: all selected tests pass; Ruff reports no errors and no formatting changes.

- [ ] **Step 5: Commit the structural redaction**

```bash
git add src/korvid/core/redaction.py tests/core/test_redaction.py \
  tests/tools/test_executor_security.py tests/mcp/test_server.py
git commit -m "fix: redact private-key fields" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Mask complete private-key PEM blocks

**Files:**
- Modify: `src/korvid/core/redaction.py:55-99`
- Modify: `src/korvid/core/redaction.py:237-273`
- Modify: `tests/core/test_redaction.py`
- Modify: `tests/agent/test_outbound.py`

**Interfaces:**
- Consumes: `redact_text(text: str, path: str, records: list[RedactionRecord]) -> str`, `record(records: list[RedactionRecord], path: str, reason: str) -> None`
- Produces: complete recognized private-key blocks replaced with `MASK_PLACEHOLDER` and recorded with reason `private-key-block`; no new public API

- [ ] **Step 1: Write failing PEM regression tests**

Append these tests near the existing free-text tests:

```python
@pytest.mark.parametrize(
    "label",
    [
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ],
)
def test_complete_private_key_pem_blocks_are_masked(label: str) -> None:
    records: list[RedactionRecord] = []
    text = (
        f"before\n-----BEGIN {label}-----\n"
        "private-key-payload-sentinel\n"
        f"-----END {label}-----\nafter"
    )

    redacted = redact_text(text, "event.message", records)

    assert redacted == f"before\n{MASK_PLACEHOLDER}\nafter"
    assert records == [
        RedactionRecord(path="event.message", reason="private-key-block"),
    ]


def test_multiple_private_key_pem_blocks_each_record_evidence() -> None:
    records: list[RedactionRecord] = []
    text = (
        "-----BEGIN PRIVATE KEY-----\nfirst-sentinel\n-----END PRIVATE KEY-----\n"
        "between\n"
        "-----BEGIN EC PRIVATE KEY-----\nsecond-sentinel\n-----END EC PRIVATE KEY-----"
    )

    redacted = redact_text(text, "log", records)

    assert "first-sentinel" not in redacted
    assert "second-sentinel" not in redacted
    assert redacted.count(MASK_PLACEHOLDER) == 2
    assert records == [
        RedactionRecord(path="log", reason="private-key-block"),
        RedactionRecord(path="log", reason="private-key-block"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN CERTIFICATE-----\ncertificate\n-----END CERTIFICATE-----",
        "-----BEGIN PUBLIC KEY-----\npublic\n-----END PUBLIC KEY-----",
        "-----BEGIN PRIVATE KEY-----\nincomplete",
        "-----BEGIN RSA PRIVATE KEY-----\nmismatch\n-----END PRIVATE KEY-----",
    ],
)
def test_non_private_or_incomplete_pem_text_is_preserved(text: str) -> None:
    records: list[RedactionRecord] = []

    assert redact_text(text, "text", records) == text
    assert records == []
```

Add this provider-boundary regression near the existing untrusted log/event
tests in `tests/agent/test_outbound.py`:

```python
def test_provider_text_boundary_masks_private_key_pem() -> None:
    records: list[RedactionRecord] = []
    text = (
        "startup failed\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "provider-private-key-sentinel\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )

    sanitized = sanitize_tool_result("get_logs", text, records=records)

    assert "provider-private-key-sentinel" not in sanitized
    assert MASK_PLACEHOLDER in sanitized
    assert records == [
        RedactionRecord(path="tool_result", reason="private-key-block"),
    ]
```

- [ ] **Step 2: Run the PEM tests and verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest \
  -p no:tach tests/core/test_redaction.py tests/agent/test_outbound.py \
  -k 'pem_blocks or multiple_private_key_pem or non_private_or_incomplete_pem or provider_text_boundary_masks_private_key_pem' -q
```

Expected: complete-block and multiple-block tests fail because the private-key payload remains visible; preservation tests pass.

- [ ] **Step 3: Add the private-key PEM pattern and replacement**

Add this compiled pattern beside the existing text-redaction patterns:

```python
_PRIVATE_KEY_PEM_RE = re.compile(
    r"-----BEGIN (?P<label>(?:(?:ENCRYPTED|RSA|EC|OPENSSH) )?PRIVATE KEY)-----"
    r".*?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)
```

Add this helper before `redact_text`:

```python
def _replace_private_key_block(
    match: re.Match[str],
    *,
    path: str,
    records: list[RedactionRecord],
) -> str:
    record(records, path, "private-key-block")
    return MASK_PLACEHOLDER
```

Update the beginning of `redact_text` so PEM masking occurs after control-character normalization and before assignment masking:

```python
    text = strip_control_characters(text, path, records)
    text = _PRIVATE_KEY_PEM_RE.sub(
        lambda match: _replace_private_key_block(match, path=path, records=records),
        text,
    )
```

Leave the existing authorization and credential-assignment substitutions unchanged.

- [ ] **Step 4: Run the complete redaction test file**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest \
  -p no:tach tests/core/test_redaction.py tests/tools/test_executor_security.py \
  tests/agent/test_outbound.py tests/mcp/test_server.py -q
```

Expected: every test in `tests/core/test_redaction.py` passes.

- [ ] **Step 5: Run targeted static checks**

Run:

```bash
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/core/redaction.py tests/core/test_redaction.py \
  tests/tools/test_executor_security.py tests/agent/test_outbound.py \
  tests/mcp/test_server.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/core/redaction.py tests/core/test_redaction.py \
  tests/tools/test_executor_security.py tests/agent/test_outbound.py \
  tests/mcp/test_server.py
MYPYPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy \
  src/korvid/core/redaction.py
```

Expected: Ruff and mypy report success.

- [ ] **Step 6: Commit PEM redaction**

```bash
git add src/korvid/core/redaction.py tests/core/test_redaction.py \
  tests/tools/test_executor_security.py tests/agent/test_outbound.py \
  tests/mcp/test_server.py
git commit -m "fix: redact private-key PEM blocks" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 3: Verify repository quality gates

**Files:**
- Modify: none
- Test: repository-wide checks

**Interfaces:**
- Consumes: completed structural and PEM redaction behavior
- Produces: evidence that issue #331 is complete without regressions

- [ ] **Step 1: Run architecture and full quality checks**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest -x -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check src tests
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check src tests
MYPYPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src
/Users/hwang-inhwan/workspace/kube/.venv/bin/tach check
```

Expected: the full test suite, Ruff, mypy, and Tach all pass.

- [ ] **Step 2: Confirm the branch contains only issue #331 changes**

Run:

```bash
git status --short
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: a clean worktree and commits limited to the #331 design and implementation.
