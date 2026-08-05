"""Fail-closed policy for data crossing the embedded-provider boundary."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from korvid.core.secrets import MASK_PLACEHOLDER, mask_secret_manifest
from korvid.tools.executor import MAX_RESULT_CHARS, compact_result
from korvid.tools.registry import tool_result_format
from korvid.tools.structured import dump_bounded_yaml, dump_yaml

_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_SENSITIVE_NAMES = frozenset(
    {
        "password",
        "token",
        "apikey",
        "authorization",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "credentials",
    }
)
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
#: Longest sensitive name in words (`access` + `token`) plus one word of
#: slack — bounds the window scan over a hostile, very long key.
_MAX_NAME_WINDOW = 3
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DOUBLE_QUOTED_VALUE = r'"(?:\\.|[^"\\\r\n])*"'
_SINGLE_QUOTED_VALUE = r"'(?:\\.|[^'\\\r\n])*'"
_AUTHORIZATION_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9])"
    r"(?P<auth_key_quote>[\"']?)authorization(?P=auth_key_quote)\s*[:=]\s*)"
    rf"(?P<value>{_DOUBLE_QUOTED_VALUE}|{_SINGLE_QUOTED_VALUE}|"
    r"(?:(?:bearer|basic)\s+)?[^\s,;}\]]+)"
)
_CREDENTIAL_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9])(?P<credential_key_quote>[\"']?)(?:"
    r"password|api[\s_-]?key|client[\s_-]?secret|access[\s_-]?token|"
    r"refresh[\s_-]?token|credentials|token"
    r")(?P=credential_key_quote)\s*[:=]\s*)"
    rf"(?P<value>{_DOUBLE_QUOTED_VALUE}|{_SINGLE_QUOTED_VALUE}|[^\s,;}}\]]+)"
)


class OutboundPolicyError(ValueError):
    """The provider request was blocked before network I/O."""


class OutboundRequestTooLarge(OutboundPolicyError):
    """The prepared request exceeded the hard character ceiling.

    Distinguished from the fail-closed content/shape blocks because it is
    recoverable: the same conversation fits again once older turns are
    dropped, so the caller can retry with less history instead of losing
    the session.
    """


#: JSON serialization is bigger than the character budget the runtime
#: accounts for, and by a variable amount: escaping doubles quotes and
#: newlines (control characters are normalized away before this point),
#: and the tool schemas ride along on every single request.
_PAYLOAD_ESCAPE_FACTOR = 2
#: Room for what message-character accounting never counts: role keys,
#: per-message envelopes, tool-call ids, and the canonical separators.
_PAYLOAD_STRUCTURE_SLACK = 8_192


def request_char_budget(*, max_history_chars: int, tools_chars: int) -> int:
    """Hard ceiling for one serialized request.

    Derived from the budgets the runtime actually enforces so the ceiling
    stays a safety net for anomalous payloads instead of a second,
    stricter budget that blocks conversations the history budget accepted.

    Args:
        max_history_chars: Retained-history budget in message characters.
        tools_chars: Serialized size of the tool schemas sent every call.

    Raises:
        ValueError: for a non-positive history budget or negative tools size.
    """
    if max_history_chars <= 0:
        raise ValueError("max_history_chars must be a positive integer")
    if tools_chars < 0:
        raise ValueError("tools_chars must not be negative")
    return max_history_chars * _PAYLOAD_ESCAPE_FACTOR + tools_chars + _PAYLOAD_STRUCTURE_SLACK


@dataclass(frozen=True)
class RedactionRecord:
    """One deterministic change made while preparing a provider request."""

    path: str
    reason: str


@dataclass(frozen=True)
class OutboundSnapshot:
    """Immutable canonical record of the exact redacted provider payload."""

    provider: str
    iteration: int
    payload_json: str
    redactions: tuple[RedactionRecord, ...]

    def export_json(self) -> str:
        """Return an inspectable JSON document containing this exact payload."""
        return (
            json.dumps(
                {
                    "provider": self.provider,
                    "iteration": self.iteration,
                    "redactions": [dataclasses.asdict(item) for item in self.redactions],
                    "payload": json.loads(self.payload_json),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


@dataclass(frozen=True)
class PreparedOutbound:
    """Sanitized provider inputs and their immutable exact snapshot."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    snapshot: OutboundSnapshot


def _blocked(reason: str) -> OutboundPolicyError:
    return OutboundPolicyError(f"outbound request blocked: {reason}")


def _normalize_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _denotes_secret(value: str) -> bool:
    """True when consecutive words of `value` spell a credential name.

    Kubernetes names are compounds (`DB_PASSWORD`, `dbPassword`,
    `github-access-token`), so exact normalization alone never recognizes
    them; splitting into words and scanning short windows does, without
    matching unrelated names that merely start the same (`TOKENIZER_PATH`).
    """
    words = tuple(word.casefold() for word in _WORD_RE.findall(value))
    return any(
        "".join(words[start : start + size]) in _SENSITIVE_NAMES
        for start in range(len(words))
        for size in range(1, min(_MAX_NAME_WINDOW, len(words) - start) + 1)
    )


def _key_path(path: str, key: str) -> str:
    if key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _record(records: list[RedactionRecord], path: str, reason: str) -> None:
    records.append(RedactionRecord(path=path, reason=reason))


def _sanitize_mapping_key(
    key: str,
    path: str,
    records: list[RedactionRecord],
) -> str:
    if not _CONTROL_RE.search(key):
        return key
    _record(records, path, "control-character")
    return _CONTROL_RE.sub("\N{REPLACEMENT CHARACTER}", key)


def _replace_match(
    match: re.Match[str],
    *,
    path: str,
    records: list[RedactionRecord],
    reason: str,
) -> str:
    _record(records, path, reason)
    value = match.group("value")
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        replacement = f"{value[0]}{MASK_PLACEHOLDER}{value[-1]}"
    else:
        replacement = MASK_PLACEHOLDER
    return f"{match.group('prefix')}{replacement}"


def _sanitize_text(text: str, path: str, records: list[RedactionRecord]) -> str:
    if _CONTROL_RE.search(text):
        text = _CONTROL_RE.sub("\N{REPLACEMENT CHARACTER}", text)
        _record(records, path, "control-character")
    text = _AUTHORIZATION_RE.sub(
        lambda match: _replace_match(
            match,
            path=path,
            records=records,
            reason="authorization-value",
        ),
        text,
    )
    return _CREDENTIAL_RE.sub(
        lambda match: _replace_match(
            match,
            path=path,
            records=records,
            reason="credential-assignment",
        ),
        text,
    )


def _secret_redactions(
    value: Mapping[str, Any],
    path: str,
    records: list[RedactionRecord],
) -> dict[str, Any]:
    try:
        masked = mask_secret_manifest(dict(value))
    except ValueError as exc:
        raise _blocked(str(exc)) from exc
    for section in ("data", "stringData"):
        entries = value.get(section)
        if isinstance(entries, Mapping):
            for key in entries:
                _record(records, _key_path(_key_path(path, section), str(key)), "secret-value")
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        annotations = metadata.get("annotations")
        if isinstance(annotations, Mapping) and _LAST_APPLIED in annotations:
            _record(
                records,
                _key_path(_key_path(_key_path(path, "metadata"), "annotations"), _LAST_APPLIED),
                "last-applied-configuration",
            )
    return masked


def _names_a_secret_sibling(value: Mapping[str, Any]) -> bool:
    """True for a `{"name": "DB_PASSWORD", "value": ...}` pair.

    Kubernetes carries container environment variables (and several
    similar list shapes) as sibling `name`/`value` keys, so the credential
    word lives in a *value*, not a key — a key-name rule alone never sees
    it and the secret ships in the sibling.
    """
    name = value.get("name")
    return isinstance(name, str) and "value" in value and _denotes_secret(name)


def _mask_reason(key: str, item: Any, *, secret_sibling: bool) -> str | None:
    """Why this entry must be masked, or None to sanitize it normally."""
    if secret_sibling and key == "value" and isinstance(item, str | int | float):
        return "sensitive-env-value"
    if _normalize_name(key) in _SENSITIVE_NAMES:
        return "sensitive-key"
    # Compound keys (`dbPassword`, `admin-api-key`) only mask text values:
    # a flag like `automountServiceAccountToken: true` names a credential
    # without carrying one, and masking it would lose real information.
    if isinstance(item, str) and _denotes_secret(key):
        return "sensitive-key"
    return None


def _sanitize_mapping(
    value: Mapping[Any, Any],
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> dict[str, Any]:
    for key in value:
        if not isinstance(key, str):
            raise _blocked("mapping keys must be strings")
    source: Mapping[str, Any] = value
    kind = source.get("kind")
    if isinstance(kind, str) and _normalize_name(kind) == "secret":
        source = _secret_redactions(source, path, records)
    secret_sibling = _names_a_secret_sibling(source)

    result: dict[str, Any] = {}
    for key, item in source.items():
        item_path = _key_path(path, key)
        if key == _LAST_APPLIED:
            _record(records, item_path, "last-applied-configuration")
            continue
        output_key = _sanitize_mapping_key(key, item_path, records)
        if output_key in result:
            raise _blocked("redacted mapping keys must remain unique")
        reason = _mask_reason(key, item, secret_sibling=secret_sibling)
        if reason is not None:
            result[output_key] = MASK_PLACEHOLDER
            _record(records, item_path, reason)
            continue
        result[output_key] = _sanitize_value(item, item_path, records, active)
    return result


def _sanitize_value(
    value: Any,
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _blocked("non-finite numbers are not allowed")
        return value
    if isinstance(value, str):
        return _sanitize_text(value, path, records)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise _blocked("recursive data structures are not allowed")
        active.add(identity)
        try:
            return _sanitize_mapping(value, path, records, active)
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise _blocked("recursive data structures are not allowed")
        active.add(identity)
        try:
            return [
                _sanitize_value(item, f"{path}[{index}]", records, active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise _blocked("unsupported outbound data type")


def _copy_tool_value(
    value: Any,
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _blocked("non-finite numbers are not allowed")
        return value
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            value = _CONTROL_RE.sub("\N{REPLACEMENT CHARACTER}", value)
            _record(records, path, "control-character")
        return value
    if isinstance(value, Mapping):
        return _copy_tool_mapping(value, path, records, active)
    if isinstance(value, list):
        return _copy_tool_list(value, path, records, active)
    raise _blocked("unsupported outbound data type")


def _copy_tool_mapping(
    value: Mapping[Any, Any],
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> dict[str, Any]:
    identity = id(value)
    if identity in active:
        raise _blocked("recursive data structures are not allowed")
    active.add(identity)
    try:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _blocked("mapping keys must be strings")
            item_path = _key_path(path, key)
            output_key = _sanitize_mapping_key(key, item_path, records)
            if output_key in result:
                raise _blocked("redacted mapping keys must remain unique")
            result[output_key] = _copy_tool_value(item, item_path, records, active)
        return result
    finally:
        active.remove(identity)


def _copy_tool_list(
    value: list[Any],
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> list[Any]:
    identity = id(value)
    if identity in active:
        raise _blocked("recursive data structures are not allowed")
    active.add(identity)
    try:
        return [
            _copy_tool_value(item, f"{path}[{index}]", records, active)
            for index, item in enumerate(value)
        ]
    finally:
        active.remove(identity)


def sanitize_screen_context(text: str) -> str:
    """Sanitize cluster-derived screen text before it enters a provider request."""
    if not isinstance(text, str):
        raise _blocked("screen context must be text")
    return _sanitize_text(text, "screen_context", [])


def _sanitize_structured_result(
    result: str,
    path: str,
    records: list[RedactionRecord],
    max_chars: int,
) -> str:
    """Redact a structured result, then bound it without breaking it.

    Order matters and is the whole point: the document is parsed and
    recursively redacted *first*, and only the redacted document is
    shrunk — structurally, so what leaves here is still parseable YAML.
    Reducing first (a byte cut) would hand this function wreckage, which
    is fail-closed blocked and takes the whole turn with it.
    """
    try:
        loaded = yaml.safe_load(result)
    except (yaml.YAMLError, RecursionError) as exc:
        raise _blocked("structured tool result is invalid YAML") from exc
    if not isinstance(loaded, dict | list):
        raise _blocked("structured tool result must be a mapping or list")
    try:
        sanitized = _sanitize_value(loaded, path, records, set())
        bounded = dump_yaml(sanitized)
        elided = len(bounded) > max_chars
        if elided:
            bounded = dump_bounded_yaml(sanitized, max_chars)
    except (RecursionError, yaml.YAMLError) as exc:
        raise _blocked("structured tool result could not be redacted") from exc
    if elided:
        _record(records, path, "size-elision")
    return bounded


def _sanitize_tool_result(
    name: str,
    result: str,
    path: str,
    records: list[RedactionRecord],
    max_chars: int | None = None,
) -> str:
    if not isinstance(name, str) or not isinstance(result, str):
        raise _blocked("tool name and result must be text")
    if tool_result_format(name) == "structured_yaml" and not result.startswith("ERROR:"):
        # A structured result is always bounded: the ingest cap applies
        # even when no tighter profile budget was given, because the
        # bound must be enforced on the *redacted* document.
        return _sanitize_structured_result(
            result,
            path,
            records,
            max_chars if max_chars is not None else MAX_RESULT_CHARS,
        )
    sanitized = _sanitize_text(result, path, records)
    if max_chars is None:
        return sanitized
    return compact_result(sanitized, max_chars)


def sanitize_tool_result(name: str, result: str, *, max_chars: int | None = None) -> str:
    """Sanitize one tool result and bound it in its own format.

    Args:
        name: Registered tool name; its result format decides the
            treatment (structured document vs. untrusted text).
        result: The raw result as the executor produced it.
        max_chars: Optional tighter budget for the sanitized result.
            Structured results are shrunk structurally (and are always
            bounded by the ingest cap); text results are head+tail
            compacted only when a budget is given, leaving the executor's
            own cap in place otherwise.
    """
    return _sanitize_tool_result(name, result, "tool_result", [], max_chars)


def provider_prepared_messages(
    provider: Any, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply a provider's dialect hook before the policy sees the messages.

    Providers whose wire format differs from the runtime's (Ollama's
    native API re-attaches `thinking`, names the executed tool, wants
    object arguments) adapt history through `LLMProvider.prepare_messages`.
    Running it here means every field an adapter adds is sanitized,
    size-checked and captured in the exact snapshot — an adapter that
    reshaped messages inside `complete` would ship content the boundary
    never saw.

    The hook receives a private deep copy, so an adapter cannot mutate the
    runtime's history, and a hook that fails or returns an unusable shape
    blocks the request rather than silently falling back.

    Args:
        provider: The provider adapter; a missing hook means the identity.
        messages: Conversation history to adapt.

    Raises:
        OutboundPolicyError: if the hook raised or returned a non-list.
    """
    private = copy.deepcopy(messages)
    hook = getattr(provider, "prepare_messages", None)
    if not callable(hook):
        return private
    try:
        prepared = hook(private)
    except Exception as exc:
        raise _blocked("provider message preparation failed") from exc
    if not isinstance(prepared, list):
        raise _blocked("provider message preparation returned an invalid shape")
    return prepared


def _sanitize_arguments(
    arguments: str,
    path: str,
    records: list[RedactionRecord],
) -> str:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return _sanitize_text(arguments, path, records)
    sanitized = _sanitize_value(parsed, path, records, set())
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sanitize_call_index(function: Mapping[Any, Any]) -> int | None:
    """Validate the native-dialect `function.index` (Ollama tool ordering)."""
    if "index" not in function:
        return None
    index = function["index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise _blocked("assistant tool call has an invalid function")
    return index


def _sanitize_call_arguments(
    arguments: Any,
    path: str,
    records: list[RedactionRecord],
) -> str | dict[str, Any]:
    """Redact tool-call arguments in whichever dialect they arrive in.

    The runtime stores arguments as a JSON string; native dialects (Ollama)
    want the parsed object. Both are sanitized the same way — the object
    form is not a hole.
    """
    if isinstance(arguments, str):
        return _sanitize_arguments(arguments, path, records)
    if isinstance(arguments, Mapping):
        sanitized = _sanitize_value(dict(arguments), path, records, set())
        if not isinstance(sanitized, dict):  # pragma: no cover - defensive
            raise _blocked("assistant tool call has an invalid function")
        return sanitized
    raise _blocked("assistant tool call has an invalid function")


def _sanitize_tool_calls(
    value: Any,
    path: str,
    records: list[RedactionRecord],
    pending: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _blocked("assistant tool_calls must be a list")
    calls: list[dict[str, Any]] = []
    for index, raw_call in enumerate(value):
        call_path = f"{path}[{index}]"
        if not isinstance(raw_call, Mapping) or set(raw_call) != {"id", "type", "function"}:
            raise _blocked("assistant tool call has an invalid shape")
        call_id = raw_call["id"]
        call_type = raw_call["type"]
        function = raw_call["function"]
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_type != "function"
            or not isinstance(function, Mapping)
            or set(function) - {"name", "arguments", "index"} != set()
            or not {"name", "arguments"} <= set(function)
        ):
            raise _blocked("assistant tool call has an invalid shape")
        name = function["name"]
        if not isinstance(name, str) or not name:
            raise _blocked("assistant tool call has an invalid function")
        call_index = _sanitize_call_index(function)
        if call_id in pending:
            raise _blocked("assistant tool call IDs must be unique")
        pending[call_id] = name
        prepared_function: dict[str, Any] = {
            "name": _sanitize_text(name, _key_path(call_path, "name"), records),
            "arguments": _sanitize_call_arguments(
                function["arguments"],
                _key_path(call_path, "arguments"),
                records,
            ),
        }
        if call_index is not None:
            prepared_function["index"] = call_index
        calls.append({"id": call_id, "type": "function", "function": prepared_function})
    return calls


def _sanitize_content(
    value: Any,
    path: str,
    records: list[RedactionRecord],
    *,
    allow_none: bool,
) -> Any:
    if value is None and allow_none:
        return None
    if not isinstance(value, str | list):
        raise _blocked("message content has an invalid type")
    return _sanitize_value(value, path, records, set())


def _sanitize_standard_message(
    raw_message: Mapping[Any, Any],
    role: str,
    path: str,
    records: list[RedactionRecord],
) -> dict[str, Any]:
    if set(raw_message) - {"role", "content", "name"} or "content" not in raw_message:
        raise _blocked(f"{role} message has an invalid shape")
    return {
        "role": role,
        "content": _sanitize_content(
            raw_message["content"],
            _key_path(path, "content"),
            records,
            allow_none=False,
        ),
    }


def _sanitize_assistant_message(
    raw_message: Mapping[Any, Any],
    path: str,
    records: list[RedactionRecord],
    pending: dict[str, str],
) -> dict[str, Any]:
    if set(raw_message) - {"role", "content", "name", "tool_calls", "thinking"}:
        raise _blocked("assistant message has an invalid shape")
    if "content" not in raw_message:
        raise _blocked("assistant message requires content")
    result: dict[str, Any] = {
        "role": "assistant",
        "content": _sanitize_content(
            raw_message["content"],
            _key_path(path, "content"),
            records,
            allow_none=True,
        ),
    }
    if "thinking" in raw_message:
        thinking = raw_message["thinking"]
        if not isinstance(thinking, str):
            raise _blocked("assistant thinking must be text")
        # Model-authored text that quotes tool results: untrusted data,
        # redacted exactly like any other outbound content.
        result["thinking"] = _sanitize_text(thinking, _key_path(path, "thinking"), records)
    if "tool_calls" in raw_message:
        result["tool_calls"] = _sanitize_tool_calls(
            raw_message["tool_calls"],
            _key_path(path, "tool_calls"),
            records,
            pending,
        )
    return result


def _sanitize_tool_message(
    raw_message: Mapping[Any, Any],
    path: str,
    records: list[RedactionRecord],
    pending: dict[str, str],
) -> dict[str, Any]:
    if set(raw_message) - {"role", "content", "name", "tool_call_id", "tool_name"}:
        raise _blocked("tool message has an invalid shape")
    call_id = raw_message.get("tool_call_id")
    content = raw_message.get("content")
    if not isinstance(call_id, str) or not isinstance(content, str):
        raise _blocked("tool message has invalid fields")
    name = pending.pop(call_id, None)
    if name is None:
        raise _blocked("tool message does not match an assistant tool call")
    result = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _sanitize_tool_result(
            name,
            content,
            _key_path(path, "content"),
            records,
        ),
    }
    if "tool_name" in raw_message:
        # Native dialects name the executed function here. It must agree
        # with the correlated call: a mismatch would mean the result is
        # sanitized under one tool's rules and attributed to another.
        tool_name = raw_message["tool_name"]
        if not isinstance(tool_name, str) or tool_name != name:
            raise _blocked("tool message names a different tool than its call")
        result["tool_name"] = _sanitize_text(tool_name, _key_path(path, "tool_name"), records)
    return result


def _sanitize_message(
    raw_message: Any,
    index: int,
    records: list[RedactionRecord],
    pending: dict[str, str],
) -> dict[str, Any]:
    path = f"messages[{index}]"
    if not isinstance(raw_message, Mapping):
        raise _blocked("each message must be a mapping")
    role = raw_message.get("role")
    if not isinstance(role, str) or role not in _ALLOWED_ROLES:
        raise _blocked("message role is not allowed")
    if pending and role != "tool":
        raise _blocked("assistant tool calls must have matching tool results")

    if role in {"system", "user"}:
        result = _sanitize_standard_message(raw_message, role, path, records)
    elif role == "assistant":
        result = _sanitize_assistant_message(raw_message, path, records, pending)
    else:
        result = _sanitize_tool_message(raw_message, path, records, pending)

    if "name" in raw_message:
        name_value = raw_message["name"]
        if not isinstance(name_value, str):
            raise _blocked("message name must be text")
        result["name"] = _sanitize_text(name_value, _key_path(path, "name"), records)
    return result


class OutboundPolicy:
    """Prepare bounded, redacted, canonical requests for embedded providers."""

    def __init__(self, max_request_chars: int) -> None:
        if (
            isinstance(max_request_chars, bool)
            or not isinstance(max_request_chars, int)
            or max_request_chars <= 0
        ):
            raise ValueError("max_request_chars must be a positive integer")
        self._max_request_chars = max_request_chars

    def prepare(
        self,
        provider: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        iteration: int,
    ) -> PreparedOutbound:
        """Validate, redact, bound, and snapshot one provider request."""
        try:
            return self._prepare(provider, messages, tools, iteration=iteration)
        except RecursionError as exc:
            raise _blocked("outbound data is too deeply nested") from exc

    def _prepare(
        self,
        provider: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        iteration: int,
    ) -> PreparedOutbound:
        if not isinstance(provider, str) or not provider:
            raise _blocked("provider name must be non-empty text")
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
            raise _blocked("iteration must be a non-negative integer")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise _blocked("messages and tools must be lists")

        records: list[RedactionRecord] = []
        pending: dict[str, str] = {}
        prepared_messages = [
            _sanitize_message(message, index, records, pending)
            for index, message in enumerate(messages)
        ]
        if pending:
            raise _blocked("assistant tool calls must have matching tool results")

        prepared_tools: list[dict[str, Any]] = []
        for index, tool in enumerate(tools):
            copied = _copy_tool_value(tool, f"tools[{index}]", records, set())
            if not isinstance(copied, dict):
                raise _blocked("each tool definition must be a mapping")
            prepared_tools.append(copied)

        payload_json = json.dumps(
            {"messages": prepared_messages, "tools": prepared_tools},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload_json) > self._max_request_chars:
            raise OutboundRequestTooLarge(
                f"outbound request exceeds {self._max_request_chars} character limit"
            )
        snapshot = OutboundSnapshot(
            provider=provider,
            iteration=iteration,
            payload_json=payload_json,
            redactions=tuple(records),
        )
        return PreparedOutbound(
            messages=prepared_messages,
            tools=prepared_tools,
            snapshot=snapshot,
        )
