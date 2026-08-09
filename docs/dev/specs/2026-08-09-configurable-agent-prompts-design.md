# Configurable agent prompts

- Date: 2026-08-09
- Status: proposed
- Related: #176 (local-model quality and prompt/tool optimization), #71 (capability profiles), #177 (eval-driven prompt tuning)

## Goal

Let a deployment adapt the agent's role statement to its own model and house
rules, without korvid shipping per-model prompt forks. With no configuration,
behaviour is exactly what korvid ships today.

The same mechanism gives the evaluation harness a first-class way to sweep
prompt variants, so better defaults are found by measurement rather than
by argument.

## Why now

#176 records two findings that pull in opposite directions:

- one shared prompt per capability tier is the right thing for korvid to
  *ship* — per-model forks are rejected "unless repeated journey results prove
  a model-family-specific protocol incompatibility";
- yet local models differ enough that the escape hatch is real, and prompt
  wording is a measurable cost on a 4k-token serving context.

Configuration resolves the tension: korvid keeps shipping two prompts, and the
deployment that hits a genuine incompatibility fixes it locally instead of
waiting for an upstream fork.

The injection points already exist. `compose_system_prompt` accepts
`system_prompt` and `ui_prompt`, and `build_profile` already selects them per
tier. This design connects configuration to seams that are already there.

## Non-goals

- Shipping per-model prompts. #176's conclusion stands.
- A template language or named-block substitution. Rejected as over-built.
- Overriding the write/no-write and UI clauses. Deferred until measurement
  shows it matters; see "Deferred" below.
- Weakening any approval, audit, or read-only behaviour. See "Security".

## Key decision: override the slot, not the composed prompt

`compose_system_prompt` assembles conditionally:

```
role statement
  + [cluster context]                  when detected
  + [UI clause]                        when UI tools are armed
  + [write clause | no-write clause]   depending on what is armed
```

The write and no-write clauses are *behavioural*, not decorative. Without
`NO_WRITE_PROMPT` a read-only deployment stops offering the equivalent
`kubectl` command and simply refuses. The conditional assembly also carries the
invariant that the model is never told about capabilities that were not armed.

Therefore configuration replaces the **role-statement slot**. Assembly stays in
code, and every conditional clause is still appended afterwards, whichever
override is active.

## Configuration surface

`~/.config/korvid/config.yaml`:

```yaml
agent:
  profile: small
  prompts:
    # Replaces the profile's role statement. Inline or file, not both.
    system: |
      You are a Kubernetes diagnostician...
    system_file: ~/.config/korvid/prompts/small-system.md

    # Appended after the role statement, before the conditional clauses.
    append: |
      House rule: never include node names in an answer.
    append_file: ~/.config/korvid/prompts/house-rules.md

    # Per-tool description overrides, applied to both profiles.
    tool_descriptions:
      get_logs: "Read recent container logs. One pod at a time."
```

`system` and `append` are independent: replacing the role statement and adding
house rules is a coherent combination.

### Slots in v1, and why

| Slot | Evidence for including it |
|---|---|
| `system` | #176's own escape hatch for model-family protocol incompatibility |
| `append` | House rules, terminology, prohibitions — common need, no new machinery |
| `tool_descriptions` | #176's measured lesson that tool/output shape moves small models more than prompt text; `SMALL_TOOL_DESCRIPTIONS` already exists for this reason but is not user-reachable |

The UI and write/no-write clauses are excluded: there is no measurement
supporting them, and #191 is a recent reminder of what it costs to build
structure ahead of evidence.

## Architecture

Three layers, each with one job, respecting the existing import rules
(`core` may import `k8s` only; `agent` may import `core`, `k8s`, `tools`).

```
core/config.py        parse YAML, read files, validate types,
                      emit warnings, produce plain strings
        |
        v
__main__.py           build PromptOverrides, validate tool names,
                      surface warnings, pass to build_profile
        |
        v
agent/profiles.py     apply overrides to the profile's slots
```

`core/config.py` must not learn about tool names (it cannot import `tools`),
and `agent/` must not learn about config files. Splitting parse from apply
keeps both rules intact and keeps file I/O in the one place that already
reports configuration problems.

### New config fields

```python
agent_prompt_system: str | None = None
agent_prompt_append: str | None = None
agent_prompt_tool_descriptions: dict[str, str] = field(default_factory=dict)
```

### New profile input

```python
@dataclass(frozen=True, slots=True)
class PromptOverrides:
    system: str | None = None
    append: str | None = None
    tool_descriptions: Mapping[str, str] = field(default_factory=dict)


def build_profile(
    name: str, *, readonly: bool, resize_supported: bool,
    overrides: PromptOverrides | None = None,
) -> AgentProfile: ...


def validate_prompt_overrides(
    profile: AgentProfile, overrides: PromptOverrides
) -> list[str]: ...
```

`validate_prompt_overrides` takes the *built* profile because both checks need
it: unknown tool names are checked against `profile.tools`, and the size guard
against `profile.system_prompt` (already overridden) and
`profile.max_history_chars`. It returns warnings rather than raising, so the
composition root decides how to surface them. It lives in `agent/` because only
that layer may import the registry.

### Tool-description precedence

user override > built-in `SMALL_TOOL_DESCRIPTIONS` > schema default.

`_trim` currently runs only for the `small` profile. It becomes the shared path
for both, with the built-in table applied only for `small`. A user overriding a
description on the `full` profile is a legitimate request.

## Error handling

Configuration problems become warnings and fall back to the shipped default;
they never crash startup. This follows the existing `agent_options_error`
precedent, where a bad `agent.options` block is reported through
`KorvidConfig.warnings`.

| Condition | Behaviour |
|---|---|
| `system` and `system_file` both present | warning; that slot falls back to the default |
| referenced file missing or unreadable | warning; slot falls back to the default |
| value present but empty or whitespace | warning; slot falls back to the default |
| non-string value | warning; slot falls back to the default |
| `tool_descriptions` names an unknown tool | warning; other entries still apply |
| system prompt exceeds the size guard | warning; the override still applies |

Both-present is treated as an error rather than silently preferring one, so an
ambiguous file never quietly wins over the inline text a reader can see.

### Size guard

Measured today, with cluster context and all clauses:

| Profile | Composed prompt | History budget | Share |
|---|---:|---:|---:|
| `full` | 2,252 chars | 120,000 | 1.9% |
| `small` | 3,149 chars | 24,000 | 13.1% |

The `small` budget is the one that bites: it is sized for a 4k-token serving
context, and a prompt that doubles starts crowding out the conversation it is
meant to guide. Warn when the profile's system prompt exceeds **25%** of the
profile's history budget — 6,000 chars for `small`, roughly twice today's —
which is loose enough not to nag and tight enough to catch a pasted essay.

The guard measures `profile.system_prompt` rather than the fully composed
string: the conditional clauses and the cluster-context note are korvid's own,
short, and not known until wiring time, so measuring them would make the same
configuration warn on one cluster and not another.

## Evaluation integrity

An override that is not recorded silently invalidates the #176 scoreboard: a
published row would no longer say which prompt produced it.

1. **Fingerprint every run.** The eval JSON gains a metadata envelope:

   ```json
   {
     "meta": {
       "profile": "small",
       "prompts": {"source": "override", "sha256": "9f2c…"}
     },
     "scenarios": [ ... ]
   }
   ```

   `source` is `default` when no override applied. `sha256` is taken over the
   composed system prompt plus the effective tool descriptions, so any change
   to either is visible.

2. **Do this now.** `report_payload` currently returns a bare list, and
   `docs/evals/results/` holds no published artifacts. Adding the envelope
   today costs one function; after the first published scoreboard row it is a
   breaking format change.

3. **Make sweeping first-class.** `python -m korvid.evals` gains
   `--system-prompt-file` and `--prompt-append-file`. Finding better defaults
   becomes a measurement loop instead of an argument.

4. **Publication rule.** `docs/evals/methodology.md` states that a publishable
   scoreboard row must carry `"source": "default"`; a row measured under an
   override must show its fingerprint and is a tuning artifact, not a
   comparable score.

## Security

The invariants in `AGENTS.md` are enforced in code, not in prompt text:

- agent write tools pass the approval gate in `ToolExecutor`/`UIBridge`;
- approval dialogs are confirmed only by user keystrokes;
- audit logging is fail-closed.

No prompt override can reach any of them. A user who instructs the model to
"delete pods without asking" gets a model that tries and is refused by the
gate, exactly as an unmodified prompt would be.

The one prompt-level property worth protecting is that the model is not told
about capabilities it was not offered. Overriding the role-statement slot
rather than the composed result preserves it: the write, no-write, and UI
clauses are still chosen by what is actually armed.

An override is local configuration, equal in trust to `agent.provider` or
`agent.base_url`, which already decide where cluster data is sent. It grants
no privilege that the config file did not already carry.

## Testing

- **Config parsing:** inline value; file value; both present; missing file;
  unreadable file; empty and whitespace-only; non-string; unknown YAML shape.
  Each asserts both the fallback and the warning text.
- **Profile application:** `system` replaces; `append` appends; both together;
  neither leaves the shipped default byte-identical; tool-description
  precedence across `full` and `small`.
- **Composition:** with an override active, the no-write clause still appears
  read-only, the write clause still names exactly the armed write tools, and
  the UI clause still appears only when UI tools are armed. This is the
  regression guard for the key decision above.
- **Validation:** unknown tool name warns and leaves other entries applied;
  an oversized prompt warns and still applies.
- **Eval:** fingerprint is `default` with no override and `override` with one;
  the same prompt yields the same hash and a changed prompt does not.

## Deferred

- UI and write/no-write clause overrides — gated on evidence that a deployment
  needs them.
- A prompt-pack directory (`~/.config/korvid/prompts/<profile>/system.md`
  applied by presence). Rejected for v1 because behaviour would change with no
  trace in the config file; `*_file` gives the same editing ergonomics while
  keeping configuration explicit.
- Per-model prompt selection inside korvid. #176 decides this, and only with
  journey results.

## Open question

Whether `append` should sit before or after the cluster-context note. This
design places the role statement and `append` together, ahead of cluster
context, so house rules read as part of the role. If house rules turn out to
need the last word, moving them is a one-line change.
