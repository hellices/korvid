"""Fail-closed policy for data crossing the embedded-provider boundary.

Shape, correlation, bounding and the exact snapshot live here — they are
about how a provider request is built. *What must never leave* lives in
`korvid.core.redaction`, one layer down, so the tool executor can apply
the identical rules where a document is produced (before any size
reduction removes the classifiers those rules read) instead of a second
copy of them drifting apart from this one.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import yaml

from korvid.core.redaction import (
    RedactionError,
    RedactionRecord,
    key_path,
    merge_records,
    rebase,
    record,
    redact_text,
    redact_value,
    sanitize_mapping_key,
)
from korvid.tools.executor import MAX_RESULT_CHARS, compact_result
from korvid.tools.registry import ResultFormat, tool_result_format
from korvid.tools.structured import (
    StructuredParseError,
    dump_bounded_yaml,
    dump_yaml,
    load_structured_document,
)

_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})


class OutboundPolicyError(ValueError):
    """The provider request was blocked before network I/O."""

    #: How the block is announced to the user. Subclasses name the
    #: boundary that actually refused, so a producer-side failure is not
    #: reported as if the outbound policy had inspected the payload.
    headline: ClassVar[str] = "outbound policy blocked the provider request"


class ToolResultBlockedError(OutboundPolicyError):
    """A tool result could not be redacted, so the turn stops before its next request.

    The refusal happens where the document is produced, not at the
    payload boundary, but it lands here so a blocked turn has exactly one
    rollback: history truncated to the turn base, carried records purged,
    the last successful snapshot left standing (PR #197 review).
    """

    headline = "the turn stopped before its next provider request"


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
class OutboundSnapshot:
    """Immutable canonical record of the exact redacted provider payload."""

    #: The model the request was addressed to. Named for what it holds:
    #: every adapter's `descriptor.model` returns its model identifier,
    #: so labelling `qwen3:8b` a "provider" told a reader of an exported
    #: payload the wrong thing about where their data went.
    model: str
    iteration: int
    payload_json: str
    redactions: tuple[RedactionRecord, ...]

    def export_json(self) -> str:
        """Return an inspectable JSON document containing this exact payload."""
        return (
            json.dumps(
                {
                    "model": self.model,
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


@contextmanager
def _fail_closed() -> Iterator[None]:
    """Translate the shared redactor's refusals into a policy block.

    `korvid.core.redaction` refuses data it cannot redact safely. Every
    public entry point here re-raises that refusal as an
    `OutboundPolicyError` so callers keep the one exception type they
    already handle (and the message the block reports stays the same).
    """
    try:
        yield
    except RedactionError as exc:
        raise _blocked(str(exc)) from exc


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
        # Schema prose is authored data too: a plugin's description or a
        # default value can carry an assignment, and control stripping
        # alone let it through to the provider (PR #197 review).
        return redact_text(value, path, records)
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
            output_key = sanitize_mapping_key(key, path, records)
            item_path = key_path(path, output_key)
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


def sanitize_screen_context(text: str, records: list[RedactionRecord] | None = None) -> str:
    """Sanitize cluster-derived screen text before it enters a provider request.

    Args:
        text: The raw screen text.
        records: Optional accumulator; the redactions applied here are
            appended to it, rooted at `screen_context`. The caller keeps
            them so the outbound inventory can report redactions whose
            evidence this pass removed rather than masked.
    """
    if not isinstance(text, str):
        raise _blocked("screen context must be text")
    with _fail_closed():
        return redact_text(text, "screen_context", records if records is not None else [])


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

    The parse itself is the strict reader: a document that YAML would
    resolve two ways is refused before redaction, because redaction reads
    the classifiers a second `kind` or `name` silently replaces.
    """
    try:
        loaded = load_structured_document(result)
    except StructuredParseError as exc:
        raise _blocked(str(exc)) from exc
    except (yaml.YAMLError, RecursionError) as exc:
        raise _blocked("structured tool result is invalid YAML") from exc
    if not isinstance(loaded, dict | list):
        raise _blocked("structured tool result must be a mapping or list")
    try:
        sanitized = redact_value(loaded, path, records, set())
        bounded = dump_yaml(sanitized)
        elided = len(bounded) > max_chars
        if elided:
            bounded = dump_bounded_yaml(sanitized, max_chars)
    except (RecursionError, yaml.YAMLError) as exc:
        raise _blocked("structured tool result could not be redacted") from exc
    if elided:
        record(records, path, "size-elision")
    return bounded


def _resolved_result_format(name: str, declared: ResultFormat | None) -> ResultFormat:
    """How this tool's results are treated, or a refusal.

    An undeclared name used to fall back to the text pass, so a custom
    tool could return a `Secret` document and ship every entry that is
    not spelled like a credential. There is no safe guess: a document
    treated as text leaks, and a paragraph treated as a document blocks
    the turn — so the caller has to have said (PR #197 review).
    """
    resolved = declared if declared is not None else tool_result_format(name)
    if resolved is None:
        raise _blocked(f"tool {name!r} has no declared result format")
    return resolved


def _sanitize_tool_result(
    name: str,
    result: str,
    path: str,
    records: list[RedactionRecord],
    max_chars: int | None = None,
    *,
    error: bool = False,
    result_format: ResultFormat | None = None,
) -> str:
    if not isinstance(name, str) or not isinstance(result, str):
        raise _blocked("tool name and result must be text")
    if not error and _resolved_result_format(name, result_format) == "structured_yaml":
        # A structured result is always bounded: the ingest cap applies
        # even when no tighter tier budget was given, because the
        # bound must be enforced on the *redacted* document.
        return _sanitize_structured_result(
            result,
            path,
            records,
            max_chars if max_chars is not None else MAX_RESULT_CHARS,
        )
    sanitized = redact_text(result, path, records)
    if max_chars is None:
        return sanitized
    return compact_result(sanitized, max_chars)


def sanitize_tool_result(
    name: str,
    result: str,
    *,
    max_chars: int | None = None,
    records: list[RedactionRecord] | None = None,
    error: bool = False,
    result_format: ResultFormat | None = None,
) -> str:
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
        records: Optional accumulator; the redactions applied here are
            appended to it, rooted at `tool_result`. The caller keeps
            them so the outbound inventory can report redactions whose
            evidence this pass removed rather than masked.
        error: Whether the producer reports this as a failure rather than
            a result. Only a producer can say so: an error is scrubbed as
            text and stays readable to the model, while everything else
            claiming a structured format is parsed and recursively
            redacted as a document. Inferring this from the text let a
            valid document opt out by starting with `ERROR:` — content
            the producer of that document chose (PR #197 review). The
            default is the safe one, so a caller that cannot tell gets
            the structural pass.
    """
    with _fail_closed():
        return _sanitize_tool_result(
            name,
            result,
            "tool_result",
            records if records is not None else [],
            max_chars,
            error=error,
            result_format=result_format,
        )


def sanitize_recorded_tool_result(
    name: str,
    result: str,
    produced: Sequence[RedactionRecord],
    *,
    max_chars: int | None = None,
    error: bool = False,
    result_format: ResultFormat | None = None,
) -> tuple[str, tuple[RedactionRecord, ...]]:
    """Sanitize one tool result and return it with its complete record trail.

    Two passes see this content: the producer, which redacts before the
    size bound and reports what it *removed*, and this one, which redacts
    what reaches history. They are two views of one document, so the
    producer's trail is re-rooted onto `tool_result` — where the content
    actually lands — and merged, reporting a mask both passes saw once
    while genuine multiplicity survives.

    One function rather than the same three lines at each call site: the
    runtime and the eval recorder both sit on this path, and an inventory
    that differed between them would make an eval run stop describing
    production (PR #197 review).
    """
    ingress: list[RedactionRecord] = []
    text = sanitize_tool_result(
        name,
        result,
        max_chars=max_chars,
        records=ingress,
        error=error,
        result_format=result_format,
    )
    return text, tuple(merge_records(ingress, [rebase(item, "tool_result") for item in produced]))


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

    Each output position must still carry its input's `role` and
    `content`. Carried ingress redaction records travel by position, so a
    hook that reordered the list — or rewrote what a position says — would
    hand each message another's inventory while the payload itself looked
    plausible. Adding dialect fields is the whole point of the hook and
    stays allowed: Ollama's `tool_name`, `thinking`, `function.index` and
    object-valued arguments all leave role and content untouched.

    That comparison is made against a baseline taken *before* the hook
    runs, deep-copied, and never handed to it. Comparing with the copy the
    hook was given would check a mutated list against itself: adapters are
    free to work in place, and an in-place reorder or rewrite would then
    agree with its own result. Holding the same content *objects* has the
    same hole one level down — content that is a list or a mapping can be
    edited where it sits (PR #197 review).

    Args:
        provider: The provider adapter; a missing hook means the identity.
        messages: Conversation history to adapt.

    Raises:
        OutboundPolicyError: if the hook raised, returned a non-list,
            changed the message count, or reordered or rewrote a message.
    """
    private = copy.deepcopy(messages)
    hook = getattr(provider, "prepare_messages", None)
    if not callable(hook):
        return private
    # A copy, not a view of the copy: a string cannot be edited in place,
    # but list- or mapping-valued content can, and holding the same object
    # the hook was handed would let an in-place edit change the baseline
    # along with the result (PR #197 review).
    baseline = copy.deepcopy([(message.get("role"), message.get("content")) for message in private])
    try:
        prepared = hook(private)
    except Exception as exc:
        raise _blocked("provider message preparation failed") from exc
    if not isinstance(prepared, list):
        raise _blocked("provider message preparation returned an invalid shape")
    if len(prepared) != len(baseline):
        # Ingress redaction records are carried by position in this list.
        # A hook that added or dropped a message would slide every record
        # after it onto the wrong message, so the request is blocked
        # rather than reported inaccurately.
        raise _blocked("provider message preparation changed the message count")
    for adapted, (role, content) in zip(prepared, baseline, strict=True):
        if not isinstance(adapted, dict) or not _same_position(adapted, role, content):
            raise _blocked("provider message preparation reordered or rewrote a message")
    return prepared


def _same_position(adapted: Mapping[str, Any], role: Any, content: Any) -> bool:
    """True when an adapted message still says what its position said.

    Identity for this purpose is `role` plus `content`: everything else a
    dialect hook touches is representation, and these two are what a
    carried record's path resolves against.

    Args:
        adapted: The message the hook produced for this position.
        role: The role this position carried before the hook ran.
        content: The content this position carried before the hook ran.

    Returns:
        True when the adapted message still carries both.
    """
    return bool(adapted.get("role") == role and adapted.get("content") == content)


def _sanitize_arguments(
    arguments: str,
    path: str,
    records: list[RedactionRecord],
) -> str:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return redact_text(arguments, path, records)
    sanitized = redact_value(parsed, path, records, set())
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
        sanitized = redact_value(dict(arguments), path, records, set())
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
        # The ID is model-authored text, so it is redacted like any other
        # model output — and the *redacted* spelling is what goes on the
        # wire and into the correlation table, because a raw ID recorded
        # here would never match the sanitized one the tool message
        # carries. Two IDs that collapse to one spelling can no longer be
        # told apart, so the uniqueness check runs after redaction.
        call_id = redact_text(call_id, key_path(call_path, "id"), records)
        if call_id in pending:
            raise _blocked("assistant tool call IDs must be unique")
        pending[call_id] = name
        prepared_function: dict[str, Any] = {
            "name": redact_text(name, key_path(call_path, "name"), records),
            "arguments": _sanitize_call_arguments(
                function["arguments"],
                key_path(call_path, "arguments"),
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
    return redact_value(value, path, records, set())


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
            key_path(path, "content"),
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
            key_path(path, "content"),
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
        result["thinking"] = redact_text(thinking, key_path(path, "thinking"), records)
    if "tool_calls" in raw_message:
        result["tool_calls"] = _sanitize_tool_calls(
            raw_message["tool_calls"],
            key_path(path, "tool_calls"),
            records,
            pending,
        )
    return result


def _sanitize_tool_message(
    raw_message: Mapping[Any, Any],
    path: str,
    records: list[RedactionRecord],
    pending: dict[str, str],
    *,
    error: bool = False,
    result_formats: Mapping[str, ResultFormat] | None = None,
) -> dict[str, Any]:
    if set(raw_message) - {"role", "content", "name", "tool_call_id", "tool_name"}:
        raise _blocked("tool message has an invalid shape")
    call_id = raw_message.get("tool_call_id")
    content = raw_message.get("content")
    if not isinstance(call_id, str) or not isinstance(content, str):
        raise _blocked("tool message has invalid fields")
    # Redacted the same way the assistant call's ID was, so the pair still
    # matches — correlation is on the spelling that ships, not the raw one.
    call_id = redact_text(call_id, key_path(path, "tool_call_id"), records)
    name = pending.pop(call_id, None)
    if name is None:
        raise _blocked("tool message does not match an assistant tool call")
    result = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _sanitize_tool_result(
            name,
            content,
            key_path(path, "content"),
            records,
            error=error,
            result_format=result_formats.get(name) if result_formats else None,
        ),
    }
    if "tool_name" in raw_message:
        # Native dialects name the executed function here. It must agree
        # with the correlated call: a mismatch would mean the result is
        # sanitized under one tool's rules and attributed to another.
        tool_name = raw_message["tool_name"]
        if not isinstance(tool_name, str) or tool_name != name:
            raise _blocked("tool message names a different tool than its call")
        result["tool_name"] = redact_text(tool_name, key_path(path, "tool_name"), records)
    return result


def _carried_records(
    index: int,
    ingress: Mapping[int, Sequence[RedactionRecord]] | None,
) -> list[RedactionRecord]:
    """Records from this message's ingress redaction, on payload paths.

    Keyed by position in the very list being prepared. The caller owns
    the projection from its own message identities onto these positions,
    because content is not an identifier: two messages that sanitize to
    the same text are still two messages, and one of them may never have
    been redacted at all.
    """
    if not ingress:
        return []
    root = key_path(f"messages[{index}]", "content")
    return [rebase(item, root) for item in ingress.get(index, ())]


def _sanitize_message(
    raw_message: Any,
    index: int,
    records: list[RedactionRecord],
    pending: dict[str, str],
    *,
    error: bool = False,
    result_formats: Mapping[str, ResultFormat] | None = None,
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
        result = _sanitize_tool_message(
            raw_message, path, records, pending, error=error, result_formats=result_formats
        )

    if "name" in raw_message:
        name_value = raw_message["name"]
        if not isinstance(name_value, str):
            raise _blocked("message name must be text")
        result["name"] = redact_text(name_value, key_path(path, "name"), records)
    return result


class OutboundPolicy:
    """Prepare bounded, redacted, canonical requests for embedded providers."""

    def __init__(
        self,
        max_request_chars: int,
        result_formats: Mapping[str, ResultFormat] | None = None,
    ) -> None:
        """Build a policy for one tool surface.

        Args:
            max_request_chars: Hard ceiling on the serialized request.
            result_formats: How each offered tool's results are treated,
                from `resolve_result_formats`. Names absent from it fall
                back to the registry, and a name neither knows is refused
                rather than assumed to be text.
        """
        if (
            isinstance(max_request_chars, bool)
            or not isinstance(max_request_chars, int)
            or max_request_chars <= 0
        ):
            raise ValueError("max_request_chars must be a positive integer")
        self._max_request_chars = max_request_chars
        self._result_formats = dict(result_formats) if result_formats else None

    def prepare(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        iteration: int,
        ingress: Mapping[int, Sequence[RedactionRecord]] | None = None,
        tool_errors: Collection[int] | None = None,
    ) -> PreparedOutbound:
        """Validate, redact, bound, and snapshot one provider request.

        Args:
            model: The model identifier the request is destined for.
            messages: Runtime history, already through the provider's
                dialect hook.
            tools: Tool schemas as the provider will receive them.
            iteration: One-based tool-loop iteration of this request:
                the first request of a turn is 1, and each tool round-trip
                that follows increments it. The value is recorded verbatim
                in the snapshot and shown by the payload inspector, so it
                is what a reader counts requests with.
            ingress: Redactions already applied to a message's content
                before it entered history, keyed by that message's index
                in `messages`. This pass re-derives what it can see, but
                a redaction that *removed* its evidence (a stripped
                control character, a deleted last-applied annotation)
                leaves nothing to find, so those records are carried in
                here and re-rooted onto the payload path they occupy.
            tool_errors: Indices of tool messages whose content the
                *producer* declared a failure rather than a result. Only a
                producer can say so, and a stored result is re-sanitized
                from scratch on every request, so the verdict has to
                travel here rather than be re-derived from the text — an
                `ERROR: API 403: ...` string re-read as a document blocked
                ordinary cluster failures (PR #197 review). An index that
                is absent gets the structural pass, so this can only ever
                relax a result the producer vouched for.
        """
        with _fail_closed():
            try:
                return self._prepare(
                    model,
                    messages,
                    tools,
                    iteration=iteration,
                    ingress=ingress,
                    tool_errors=tool_errors,
                )
            except RecursionError as exc:
                raise _blocked("outbound data is too deeply nested") from exc

    def _prepare(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        iteration: int,
        ingress: Mapping[int, Sequence[RedactionRecord]] | None = None,
        tool_errors: Collection[int] | None = None,
    ) -> PreparedOutbound:
        if not isinstance(model, str) or not model:
            raise _blocked("model name must be non-empty text")
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
            raise _blocked("iteration must be a non-negative integer")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise _blocked("messages and tools must be lists")

        records: list[RedactionRecord] = []
        pending: dict[str, str] = {}
        prepared_messages = []
        for index, message in enumerate(messages):
            own_start = len(records)
            prepared_messages.append(
                _sanitize_message(
                    message,
                    index,
                    records,
                    pending,
                    error=tool_errors is not None and index in tool_errors,
                    result_formats=self._result_formats,
                )
            )
            carried = _carried_records(index, ingress)
            if carried:
                own = records[own_start:]
                del records[own_start:]
                records.extend(merge_records(own, carried))
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
            model=model,
            iteration=iteration,
            payload_json=payload_json,
            redactions=tuple(records),
        )
        return PreparedOutbound(
            messages=prepared_messages,
            tools=prepared_tools,
            snapshot=snapshot,
        )
