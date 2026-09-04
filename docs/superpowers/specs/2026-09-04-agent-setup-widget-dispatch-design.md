# Agent Setup Widget Dispatch Design

## Goal

Replace the two widget-ID `if`/`elif` chains in `AgentSetupScreen` with typed
bound-handler dispatch while preserving the setup wizard's state transitions,
validation, retained settings, focus, and worker ordering.

## Scope

This change covers only:

- `on_option_list_option_selected()`
- `on_input_submitted()`

It extracts the four option actions and four input actions into private methods.
It does not redesign the wizard as a state machine and does not change
`on_input_changed()`, `on_key()`, provider defaults, model loading, connection
testing, saving, or retry behavior.

## Considered Approaches

### Typed bound-handler maps

Each public Textual handler stops the event, looks up a bound private method by
widget ID, and calls it when present.

This is the chosen approach because every entry in one map consumes the same
event type:

```python
_OptionHandler = Callable[[OptionList.OptionSelected], None]
_InputHandler = Callable[[Input.Submitted], None]
```

The maps are built from `self._...` methods at event time. That preserves
subclass overrides and test probes instead of capturing base-class functions at
module import time.

### `match` plus extracted handlers

`match event.option_list.id` would preserve type narrowing, but the cases would
still repeat the stable ID-to-handler association. It provides less extension
and test value than a typed map when all handlers already share one signature.

### Wizard state machine

A formal state machine could validate legal transitions, but it would couple
this dispatch cleanup to a behavioral redesign. Existing flows include retained
provider settings, GitHub device login, Azure authentication, filtered model
selection, retries, and asynchronous workers. Changing those transitions is
outside this refactor.

## Components

### Public dispatchers

`on_option_list_option_selected(event)`:

1. Calls `event.stop()` exactly once.
2. Reads `event.option_list.id`.
3. Returns without action if the ID is `None` or unknown.
4. Builds the typed option-handler map from the current instance.
5. Calls the selected handler with the original event.

`on_input_submitted(event)` follows the same steps using `event.input.id` and
the input-handler map.

### Option handlers

- `_select_provider(event)` contains the existing provider branch unchanged.
- `_select_auth(event)` contains the existing auth branch unchanged.
- `_select_model_option(event)` forwards the selected prompt to
  `_choose_model()`.
- `_select_tier_option(event)` forwards the selected option ID or
  `"automatic"` to `_choose_tier()`.

### Input handlers

- `_submit_base_url(event)` contains the existing endpoint branch unchanged.
- `_submit_api_key_env(event)` contains the existing API-key environment branch
  unchanged.
- `_submit_model_filter(event)` chooses the highlighted model only when a
  highlight and options exist.
- `_submit_model(event)` preserves the required-model validation and status
  message.

### Handler-map construction

Private methods return fresh typed maps:

```python
def _option_handlers(self) -> dict[str, _OptionHandler]:
    return {
        "setup-provider": self._select_provider,
        "setup-auth": self._select_auth,
        "setup-model-list": self._select_model_option,
        "setup-tier": self._select_tier_option,
    }
```

The input map follows the same pattern. Fresh maps are intentionally small and
avoid a class-level registry of unbound methods, string-based `getattr`, casts,
or `Any`.

## Behavioral Contracts

- Unknown or missing widget IDs remain stopped no-ops.
- Provider selection hides the provider list before advancing.
- Azure selection displays and focuses the auth list, retaining Entra when
  reconnecting.
- Non-Azure providers retain a configured auth method when reconnecting.
- GitHub Copilot still starts its connection worker from `_after_auth_method()`.
- Endpoint submission stores the stripped URL or `None` before advancing.
- API-key auth still displays, prefills, and focuses the environment input.
- Non-API-key auth still starts model fetching immediately.
- API-key environment submission stores the stripped value or `None` before
  starting model fetching.
- Model-filter submission selects only the current highlighted option.
- Empty direct model submission displays `"Model is required"` and does not
  advance.
- Model and tier option prompts/IDs are forwarded unchanged.
- Worker calls retain their existing `exclusive=True` setting and ordering.

## Error Handling

The public dispatchers do not catch handler exceptions. Existing errors continue
to surface through Textual and the existing worker error paths.

Unknown IDs do not raise because Textual may deliver events from future or
embedded widgets. This preserves the current behavior of falling through the
`if`/`elif` chain after stopping the event.

Validation remains inside the action that owns it. In particular, empty model
input is not converted into dispatcher-level validation.

## Testing

The existing `tests/ui/test_agent_setup_screen.py` flow tests remain the primary
behavior contract. Add focused dispatch regressions using an
`AgentSetupScreen` subclass:

1. Override all four option handlers and prove each known widget ID reaches the
   correct bound override.
2. Override all four input handlers and prove each known input ID reaches the
   correct bound override.
3. Send an event from an unknown option/input ID and prove no action handler is
   called while the event is stopped.
4. Preserve all existing end-to-end tests for Ollama, GitHub Copilot, Azure,
   filtering, reconnect prefill, tier selection, retry, save, and error paths.

Tests must not replace production methods with untyped monkeypatches or use
`Any`, casts, or `type: ignore` to satisfy the handler types.

## Non-Goals

- Defining a formal wizard state enum or reducer.
- Rejecting out-of-order Textual events beyond current behavior.
- Changing widget IDs or visible copy.
- Changing provider aliases or defaults.
- Changing asynchronous worker ownership or exclusivity.
- Refactoring model filtering or keyboard navigation.
- Combining this work with eval tally deduplication.
