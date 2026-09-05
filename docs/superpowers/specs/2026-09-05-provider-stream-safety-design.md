# Provider Stream Safety Design

## Context

Built-in providers currently treat transport EOF as successful completion.
OpenAI-compatible streams can therefore emit `done` without `[DONE]`, while
Ollama streams can emit `done` without a chunk whose `done` field is true.
Ollama also keeps reading chunks after a terminal chunk. Before the agent
runtime can enforce its response policy, the adapters accumulate tool-call
arguments, native tool calls, and Ollama reasoning in memory. The provider
configuration probe similarly concatenates response text without a bound.

Issue #336 makes each built-in adapter responsible for its protocol terminal
contract and for bounding memory that only the adapter can see.

## Goals

- Require an explicit protocol terminal marker before emitting successful
  completion.
- Stop consuming a stream at its first terminal marker.
- Bound OpenAI tool arguments, Ollama tool arguments, Ollama reasoning, native
  tool-call count, and connection-test response text.
- Raise the existing typed `ProviderError` when a stream is truncated or a
  provider-local limit is exhausted.
- Preserve request-sent, text delta, usage, tool-call, and close semantics for
  valid streams.

## Non-goals

- Changing the public `LLMProvider` event contract.
- Adding user-configurable provider buffer limits.
- Bounding rendered answer text in the adapters; the engine already owns that
  visible response policy.
- Changing third-party provider plugin limits.
- Retrying a malformed or truncated stream.

## Design

### Shared built-in limits

Add a small private provider helper containing fixed limits and UTF-8 byte
measurement. The limits are:

- 64 native tool calls per response;
- 65,536 UTF-8 bytes of serialized arguments per tool call;
- 262,144 UTF-8 bytes of Ollama reasoning per response;
- 16,384 UTF-8 bytes of connection-test text.

The tool-argument limit matches the provider plugin contract. The larger
reasoning limit allows useful native reasoning while keeping hidden memory
bounded. The probe limit is intentionally small because its prompt asks for one
word.

Limit checks happen before appending to an accumulator. Exceeding a limit
raises `ProviderError`; adapters never truncate data and continue as if the
response were complete.

### OpenAI-compatible SSE

`OpenAICompatProvider.complete` tracks whether `[DONE]` was observed. It stops
at the first marker and ignores all subsequent transport data by breaking the
line iterator immediately. EOF before the marker raises `ProviderError` before
accumulated tool calls, usage, or the final `done` event are emitted.

Tool call indices are limited to 64 distinct calls. Each accumulated argument
string is checked by UTF-8 byte length as fragments arrive. Malformed JSON
continues to raise rather than being converted to success.

### Ollama NDJSON

`OllamaProvider.complete` stops at the first chunk where `done is True`.
Transport EOF without that exact terminal value raises `ProviderError`.
The terminal chunk's own `message` payload is processed once before the
adapter records usage and breaks, so valid final content, reasoning, and tool
calls are preserved while any later chunks are ignored.

Reasoning is checked cumulatively by UTF-8 bytes. Native tool calls are checked
before serialization and append: no more than 64 calls, and each serialized
arguments object is at most 65,536 UTF-8 bytes. On any limit failure, no
remembered reasoning, accumulated tool calls, usage, or final `done` event is
emitted.

### Connection probe

`ProviderConfigurator.test` checks cumulative UTF-8 bytes for text deltas before
concatenation. If the 16,384-byte limit would be exceeded, it raises
`ProviderError`. The existing `finally` block still closes the provider.
Successful empty responses retain the existing `RuntimeError` contract.

## Error semantics

Protocol truncation and provider-local limit exhaustion are upstream provider
contract failures, so both use `ProviderError` with messages that name the
protocol condition or exhausted limit without including accumulated content.
HTTP errors keep their existing `ProviderError` behavior. Cancellation is not
caught or translated.

## Testing

Use TDD in the existing provider test modules:

- OpenAI: missing `[DONE]`, post-terminal data ignored, cumulative argument
  bytes, and tool-call count.
- Ollama: missing `done: true`, post-terminal data ignored, cumulative
  reasoning bytes, serialized argument bytes, and tool-call count.
- Configurator: cumulative probe text limit and provider closure on failure.
- Existing valid-stream and HTTP-error tests remain green.

Run targeted provider tests while iterating, then Ruff, mypy, Tach, the full
repository test suite, and `uv.lock` integrity checks before delivery.
