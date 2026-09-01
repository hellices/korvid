# Private-Key Redaction Design

- Date: 2026-08-31
- Issue: #331
- Status: Approved for implementation

## Goal

Prevent private-key material in Kubernetes resources and provider-bound
free-form cluster text from leaving Korvid. Extend the existing shared
redaction primitive without adding unrelated credential heuristics. MCP
resource reads benefit immediately through `ToolExecutor`; producer-side
redaction of MCP log/event text remains issue #330.

## Priority and sequencing

Issue #331 is the first implementation in the local-first MCP safety baseline.
It closes a direct secret-exfiltration path and strengthens the primitive that
issue #330 will reuse for log and event output. The active sequence is:

1. #331 shared redaction foundation
2. #330 MCP log/event producer redaction
3. #332, #333, and #334 mandatory local write safety
4. #335 only if Helm mutations ship
5. #336 provider stream safety
6. #344 Kubernetes error boundary
7. #329 and #346 deployed together as the stdio-default transition
8. #342 as an optional small crash fix

Issues labeled `status: backlog` are excluded from this active queue. Shipping
#329 alone would force existing direct HTTP clients to manage the internal
capability. Shipping it with #346 keeps that credential internal: users
configure stdio and never see the token.

## Approaches considered

### 1. Extend the shared redactor (selected)

Add proven private-key field names and recognized PEM private-key blocks to
`korvid.core.redaction`. Structural and free-form consumers receive the same
behavior and deterministic evidence.

This keeps one security boundary, preserves the existing fail-closed contract,
and gives issue #330 reusable text redaction.

### 2. Redact only in MCP serialization

This would close one outbound path but leave embedded providers and other
consumers exposed. It would also duplicate security logic at a transport
boundary that lacks Kubernetes structure.

### 3. Mask every key-related name and PEM block

This is simple but over-redacts useful public-key identifiers, certificates,
and incomplete diagnostic text. It conflicts with the issue requirement to
keep harmless public-key identifiers readable.

## Design

### Structural field names

Extend the existing normalized sensitive-name vocabulary with `privatekey` and
the Kubernetes client-config spelling `clientkeydata`. The current word-window
logic then recognizes exact and compound variants such as `privateKey`,
`private_key`, `client-private-key`, and `client-key-data` without classifying
generic key-data fields.

Do not classify `publicKey`, `publicKeyId`, `secretKeyRef`, or generic `key`
fields as private-key material. The existing one-bit exception preserves
Boolean values only for compound names recognized by the word-window scan;
exact normalized sensitive names such as `privateKey` and `client-key-data`
remain masked regardless of value type.

Structural matches use the existing `sensitive-key` reason and replace the
entire value before descending into it. This preserves deterministic record
paths and blocks malformed structured values as well as strings.

### PEM private-key blocks

Add a bounded multiline pattern for complete private-key PEM blocks with these
recognized labels:

- `PRIVATE KEY` (PKCS#8)
- `ENCRYPTED PRIVATE KEY` (encrypted PKCS#8)
- `RSA PRIVATE KEY` (PKCS#1)
- `EC PRIVATE KEY`
- `OPENSSH PRIVATE KEY` (the default `ssh-keygen` private-key format and a
  concrete Kubernetes `kubernetes.io/ssh-auth` secret payload)

The pattern begins at a matching `BEGIN` line and ends only at the matching
`END` label. It replaces the complete block with the existing mask placeholder
and records `private-key-block` at the text path.

Certificates, public-key PEM blocks, unrelated text, and incomplete or
mismatched private-key blocks remain unchanged. The pattern does not attempt
to validate base64 payloads; header and footer agreement is the proven format
boundary requested by the issue.

PEM masking runs after control-character normalization and before
assignment-pattern masking. This removes the largest secret first while
allowing surrounding credential assignments to retain their existing behavior.

## Data flow

1. A producer calls `redact_document`, `redact_manifest`, or `redact_text`.
2. Structural mappings classify private-key field names and mask their values.
3. Every string passes through control-character normalization.
4. Complete recognized PEM private-key blocks are replaced and recorded.
5. Existing authorization and credential-assignment patterns run on the
   remaining text.
6. The caller receives redacted data and the existing deterministic
   `RedactionRecord` inventory.

`ToolExecutor.get_resource` already applies structural redaction before
bounding and carries records in `ToolOutcome`. The MCP server returns that
producer output directly, while `agent.outbound` re-applies structural or text
redaction as a final provider defense. No consumer-specific wiring or new
dependency is required for #331. `ToolExecutor.get_logs` and `get_events`
currently assemble raw text, so MCP protection for those producers is
explicitly deferred to #330.

## Error handling

The change preserves current fail-closed behavior for unsupported document
shapes. PEM processing is deterministic and local; it does not decode or parse
key material and introduces no fallback that could return the original text
after a redaction error.

## Testing

Use TDD in `tests/core/test_redaction.py`.

- Prove normalized structural variants are masked, including nested and
  structured values.
- Prove `publicKey`, `publicKeyId`, and `secretKeyRef` remain readable.
- Prove complete plain/encrypted PKCS#8, PKCS#1 RSA, EC, and OpenSSH private-key blocks
  are masked.
- Prove surrounding text and multiple private-key blocks are handled.
- Prove certificates, public-key blocks, incomplete blocks, and mismatched
  headers/footers remain unchanged.
- Assert deterministic record paths and reasons.
- Prove `ToolExecutor.get_resource` applies the shared primitive before output
  reaches its size bound.
- Prove the provider outbound text boundary masks private-key PEM material.
- Prove an MCP resource read returns the already-redacted executor result.

Run the targeted redaction tests and Ruff while iterating. Before completion,
run the relevant full quality gate required by the repository workflow.

## Non-goals

- New generic entropy or secret-detection heuristics
- Public-key or certificate redaction
- MCP authentication or stdio transport
- Producer-side log/event integration from issue #330
