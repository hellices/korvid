"""Live eval CLI: `python -m korvid.evals` (issue #69).

Runs the bundled (or a custom) scenario pack against a live model
endpoint and prints a markdown report. This is a manual, on-demand tool —
it talks to a real model and is never part of CI (CI covers the harness
itself with scripted-provider smoke tests).

The provider is built by exactly the factory the TUI uses
(`create_provider_from_profile`), from a `ModelConnectionConfig` assembled
out of the variables below. That is the whole point: a score is only
evidence about korvid if the run went through korvid's own construction
path, with the same routing, credential resolution, capability lookup,
option filtering and TLS trust.

Configuration comes from the environment:

- `KORVID_EVAL_BASE_URL` — endpoint base URL (required)
- `KORVID_EVAL_MODEL` — model reference, `provider/model` (required)
- `KORVID_EVAL_PROVIDER` — compatibility only: the prefix to put in front
  of `KORVID_EVAL_MODEL` when that value has no `/`. Never a transport
  choice; korvid takes no branch on its value.
- `KORVID_EVAL_API_KEY_ENV` — the *name* of the variable holding the key
- `KORVID_EVAL_API_KEY` — **deprecated**: the key itself. Still honoured,
  and still read by name (the profile stores `KORVID_EVAL_API_KEY`, never
  its value), but it puts a credential in the eval's own environment
  namespace. Prefer `KORVID_EVAL_API_KEY_ENV`.
- `KORVID_EVAL_OPTIONS_JSON` — a JSON object of profile options
  (`temperature`, `num_ctx`, …), exactly as a connection profile's
  own `options` block
- `KORVID_EVAL_CA_BUNDLE` — the eval's `network.ca_bundle`
- `KORVID_EVAL_TIMEOUT_SECONDS` — request timeout for slow local models
  (default 60), carried as the `timeout` profile option. It wins over a
  `timeout` inside `KORVID_EVAL_OPTIONS_JSON`; either way the value must
  be a positive, finite number of seconds or the run is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final

from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.model_profiles import (
    ConnectionAuthConfig,
    ModelConnectionConfig,
    split_reference,
)
from korvid.agent.provider import LLMProvider
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.harness import (
    NO_GRIND,
    PromptGrind,
    UnknownEvalToolError,
    armed_tool_names,
    eval_surface_names,
    grind_layer_ids,
    resolve_eval_policy,
    static_prompt,
    tier_prompt_id,
)
from korvid.evals.interaction import interaction_payload
from korvid.evals.runner import (
    DEFAULT_REPETITIONS,
    ScenarioReport,
    render_markdown,
    run_scenario,
)
from korvid.evals.scenario import Scenario, bundled_scenarios_dir, load_scenarios
from korvid.evals.serving import ProbeResult, ollama_root, serving_metadata
from korvid.providers.litellm_catalog import LiteLLMModelCatalog
from korvid.providers.litellm_factory import create_provider_from_profile
from korvid.providers.litellm_runtime import models_by_provider
from korvid.providers.special_flows import SpecialFlowRegistry
from korvid.tools.executor import ToolExecutor

#: The reference separator, spelled once. `provider/model` is the shape
#: every korvid profile uses; the eval's legacy two-variable form is
#: joined into it rather than interpreted.
_REFERENCE_SEPARATOR: Final = "/"

#: Seconds a live eval waits for a model that has not answered yet. Local
#: 30B-class models on cold weights routinely exceed any SDK default.
DEFAULT_EVAL_TIMEOUT_SECONDS: Final = 60.0

#: The deprecated variable that holds the credential *value*. Kept working,
#: but the profile only ever stores this name — the value is read by the
#: production `environment` auth method, from the process environment,
#: exactly as it would be for a TUI profile.
_LEGACY_API_KEY_VAR: Final = "KORVID_EVAL_API_KEY"

#: What `litellm_factory._refuse` appends to every refusal. Trimmed off the
#: text the CLI prints because "the agent is disabled" describes the TUI,
#: not an eval run. Trimming a suffix that is no longer there is a no-op,
#: so a reworded refusal still reaches the operator in full.
_FACTORY_REFUSAL_SUFFIX: Final = " — the agent is disabled"

_FACTORY_LOGGER: Final = "korvid.providers.litellm_factory"


def _eval_reference(env: Mapping[str, str]) -> str:
    """The model reference, canonical if given, joined if not.

    `KORVID_EVAL_PROVIDER` predates canonical references. It survives as a
    *prefix*, never as a choice: whatever the operator wrote is joined to
    the model with a separator and handed to the same routing the TUI
    uses, so no vendor name is ever compared here.
    """
    model = env.get("KORVID_EVAL_MODEL", "").strip()
    if _REFERENCE_SEPARATOR in model:
        return model
    prefix = env.get("KORVID_EVAL_PROVIDER", "").strip()
    if not prefix or not model:
        return model
    return f"{prefix}{_REFERENCE_SEPARATOR}{model}"


def _eval_auth(env: Mapping[str, str]) -> ConnectionAuthConfig:
    """The auth the eval profile declares — a variable *name*, or nothing.

    Both supported forms resolve to the `environment` method, so the
    profile carries no secret and the credential is read by the same code
    path a TUI profile uses.
    """
    named = env.get("KORVID_EVAL_API_KEY_ENV", "").strip()
    if not named and env.get(_LEGACY_API_KEY_VAR, "").strip():
        # One string literal, read from nothing. This warning fires only
        # when an inline credential is in scope, and CodeQL reports any
        # credential-shaped expression at an output sink as clear-text
        # logging of the credential itself (alert #12,
        # `py/clear-text-logging-sensitive-data`) — interpolating the
        # legacy variable's *name* here did exactly that. Naming the
        # replacement keeps the notice actionable; the deprecated
        # spelling is named in this module's docstring and in
        # `docs/evals/methodology.md`, neither of which is an output
        # stream.
        print(
            "warning: an inline eval API key variable is set; that form is"
            " deprecated and will be removed. Set KORVID_EVAL_API_KEY_ENV to"
            " the name of the variable holding the key instead.",
            file=sys.stderr,
        )
        named = _LEGACY_API_KEY_VAR
    if not named:
        return ConnectionAuthConfig(method="none")
    return ConnectionAuthConfig(method="environment", settings={"key": named})


def _eval_options(env: Mapping[str, str]) -> dict[str, object]:
    """Profile options from `KORVID_EVAL_OPTIONS_JSON`, plus the timeout.

    The timeout is an option rather than a transport argument on purpose:
    it travels the shared `RequestPlan` boundary as a named `acompletion`
    parameter, so the eval and the TUI bound their requests identically.

    Precedence, in order: an explicit `KORVID_EVAL_TIMEOUT_SECONDS`, then a
    `timeout` key inside the JSON, then `DEFAULT_EVAL_TIMEOUT_SECONDS`.
    Whichever spelling supplies it, the value is validated *here*: a
    timeout `build_plan` cannot use is dropped there, and a dropped timeout
    that had already suppressed the default leaves the run unbounded —
    which is the opposite of what writing a timeout asks for.
    """
    raw = env.get("KORVID_EVAL_OPTIONS_JSON", "").strip()
    options: dict[str, object] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise SystemExit(f"KORVID_EVAL_OPTIONS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit("KORVID_EVAL_OPTIONS_JSON must be a JSON object of profile options.")
        options.update(parsed)

    raw_timeout = env.get("KORVID_EVAL_TIMEOUT_SECONDS", "").strip()
    if raw_timeout:
        options["timeout"] = _eval_timeout_seconds(raw_timeout)
    elif "timeout" in options:
        options["timeout"] = _option_timeout_seconds(options["timeout"])
    else:
        options["timeout"] = DEFAULT_EVAL_TIMEOUT_SECONDS
    return options


def _eval_timeout_seconds(raw: str) -> float:
    """`KORVID_EVAL_TIMEOUT_SECONDS`, whose value is text by definition."""
    source = "KORVID_EVAL_TIMEOUT_SECONDS"
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise SystemExit(_timeout_refusal(source, raw)) from exc
    return _checked_timeout(seconds, source=source, value=raw)


def _option_timeout_seconds(value: object) -> float:
    """The `timeout` key inside `KORVID_EVAL_OPTIONS_JSON`.

    JSON is typed, so a quoted number is a type error rather than
    something to parse: `build_plan` applies exactly that rule to a
    profile's own options, and this variable *is* a profile's options
    block. `bool` is not a duration either, however int-like it is.
    """
    source = 'KORVID_EVAL_OPTIONS_JSON option "timeout"'
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SystemExit(_timeout_refusal(source, value))
    return _checked_timeout(float(value), source=source, value=value)


def _checked_timeout(seconds: float, *, source: str, value: object) -> float:
    if not math.isfinite(seconds) or seconds <= 0:
        raise SystemExit(_timeout_refusal(source, value))
    return seconds


def _timeout_refusal(source: str, value: object) -> str:
    """One wording for every spelling: only the source that carried it differs."""
    return (
        f"{source} must be a positive, finite number of seconds (for example 900); got {value!r}."
    )


def eval_model_tag(env: Mapping[str, str]) -> str:
    """The model as the *serving endpoint* names it.

    The routing prefix in `provider/model` is korvid's own vocabulary: an
    endpoint's metadata API knows `qwen3:8b`, not `ollama/qwen3:8b`, and
    answers nothing for the prefixed form — so digest, quantization and
    context length go silently unpinned. The reference is split by the
    shared `split_reference`, which takes no vendor branch: whatever
    precedes the first separator is dropped for the probe, whoever the
    provider is.
    """
    return split_reference(_eval_reference(env))[1]


def eval_api_key(env: Mapping[str, str]) -> str:
    """The credential the *serving probe* presents, resolved like the profile's.

    The probe is metadata collection, not the eval itself, so it reads the
    same two variables rather than growing its own convention.
    """
    named = env.get("KORVID_EVAL_API_KEY_ENV", "").strip()
    if named:
        return os.environ.get(named, "").strip()
    return env.get(_LEGACY_API_KEY_VAR, "").strip()


class _RefusalCollector(logging.Handler):
    """Keeps the factory's refusals so the CLI can exit *saying* one.

    `create_provider_from_profile` logs its reason and returns None,
    because a misconfigured profile must not stop the TUI from starting.
    A CLI has the opposite obligation: there is nothing to degrade to, so
    the reason has to reach the operator's terminal.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.reasons.append(record.getMessage().removesuffix(_FACTORY_REFUSAL_SUFFIX))


def eval_profile_from_env(env: Mapping[str, str]) -> ModelConnectionConfig:
    """The connection profile the `KORVID_EVAL_*` variables describe.

    A plain `ModelConnectionConfig` — the same shape a configured
    connection parses into — so the eval has no configuration vocabulary
    of its own. It never holds a credential: `auth` names the variable,
    and the production `environment` method reads it.

    Raises:
        SystemExit: The endpoint or the model reference is missing, or an
            option value cannot be understood.
    """
    base_url = env.get("KORVID_EVAL_BASE_URL", "").strip()
    reference = _eval_reference(env)
    if not base_url or not reference:
        raise SystemExit(
            "korvid.evals needs a live model endpoint: set KORVID_EVAL_BASE_URL"
            " and KORVID_EVAL_MODEL (and KORVID_EVAL_API_KEY_ENV if required)."
        )
    return ModelConnectionConfig(
        model=reference,
        endpoint=base_url,
        auth=_eval_auth(env),
        options=_eval_options(env),
    )


def provider_factory_from_env(env: Mapping[str, str]) -> Callable[[], LLMProvider]:
    """Build a live-provider factory from `KORVID_EVAL_*` variables.

    The variables become one `ModelConnectionConfig`, and every call to
    the returned factory hands that profile to the same
    `create_provider_from_profile` the TUI's composition root calls, with
    the same entry-point `SpecialFlowRegistry` and the same
    `LiteLLMModelCatalog` built over it. Reference validation, special-flow
    claims, credential resolution, endpoint rules, capability lookup,
    option filtering and CA-bundle trust are therefore not reimplemented
    here — they are the product's, unmodified.

    Args:
        env: The variables to read. `main` passes `os.environ`; the
            credential itself is always read from the process environment
            by the profile's `environment` auth method, so a caller that
            passes a literal mapping still gets production credential
            semantics.

    Returns:
        A factory returning a fresh provider per call — a repetition must
        never inherit another repetition's transport state.

    Raises:
        SystemExit: The variables are incomplete or contradictory, or the
            profile they describe is one korvid refuses to build. The
            refusal happens here rather than at the first repetition: a
            campaign that cannot build its provider must fail before it
            creates an artifact directory, not hours into a GPU run.
    """
    profile = eval_profile_from_env(env)
    ca_bundle = env.get("KORVID_EVAL_CA_BUNDLE", "").strip() or None

    # Discovered once per campaign rather than once per repetition: entry
    # points cannot change mid-run, and `models_by_provider()` sorts every
    # shipped model id on each call. The provider is still rebuilt every
    # time, which is the part that has to be fresh.
    flows = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    catalog = LiteLLMModelCatalog(flows=flows)

    def build() -> LLMProvider:
        collector = _RefusalCollector()
        logger = logging.getLogger(_FACTORY_LOGGER)
        restore_level = logger.level
        if not logger.isEnabledFor(logging.WARNING):
            logger.setLevel(logging.WARNING)
        logger.addHandler(collector)
        try:
            provider = create_provider_from_profile(
                profile,
                catalog=catalog,
                flows=flows,
                ca_bundle=ca_bundle,
            )
        finally:
            logger.removeHandler(collector)
            logger.setLevel(restore_level)
        if provider is None:
            reason = "; ".join(collector.reasons) or "the profile was refused"
            raise SystemExit(
                f"korvid.evals cannot build a provider for {profile.model!r}: {reason}"
            )
        return provider

    # Built now, handed out on the first call. Validating by building and
    # discarding would run an installed flow's `build_provider` an extra
    # time, and a flow is allowed to do real work there (a device login,
    # for one). This provider has never been used, so the first repetition
    # still gets a clean one.
    validated: LLMProvider | None = build()

    def factory() -> LLMProvider:
        nonlocal validated
        if validated is not None:
            provider, validated = validated, None
            return provider
        return build()

    return factory


Fetch = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]]


async def probe_serving(base_url: str, model: str, *, fetch: Fetch) -> ProbeResult:
    """Ask the serving endpoint what it is, without ever failing the run.

    The probe is metadata collection for reproducibility (#235). A campaign
    that has already spent hours of GPU time must not die because an
    endpoint does not implement ollama's native API, so every call is
    tolerated individually and whatever answered is kept.
    """
    root = ollama_root(base_url)
    payloads: dict[str, dict[str, Any] | None] = {
        "version": None,
        "show": None,
        "tags": None,
        "ps": None,
    }
    requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("version", f"{root}/api/version", None),
        ("show", f"{root}/api/show", {"model": model}),
        ("tags", f"{root}/api/tags", None),
        # Last, and after any warm-up: it reports the runtime context
        # allocation, which only exists while the model is loaded.
        ("ps", f"{root}/api/ps", None),
    ]
    errors: list[str] = []
    for name, url, body in requests:
        try:
            payloads[name] = await fetch(url, body)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return ProbeResult(
        version=payloads["version"],
        show=payloads["show"],
        tags=payloads["tags"],
        ps=payloads["ps"],
        error="; ".join(errors) or None,
    )


def httpx_fetch(*, api_key: str, timeout_seconds: float) -> Fetch:
    """A `Fetch` backed by the same hardened client the provider uses."""

    async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        import httpx

        from korvid.providers.net import make_client

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with make_client(None, httpx.Timeout(timeout_seconds, connect=10.0)) as client:
            response = (
                await client.get(url, headers=headers)
                if payload is None
                else await client.post(url, json=payload, headers=headers)
            )
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"expected a JSON object from {url}")
        return body

    return fetch


async def capture_serving(
    base_url: str,
    model: str,
    *,
    fetch: Fetch,
    warmup: bool,
    warmup_fetch: Fetch | None = None,
) -> dict[str, Any]:
    """Warm up if asked, then record what served the run.

    The warm-up runs first so `/api/show` reports a loaded model. It gets
    its own fetcher because paging a 30B model off disk takes minutes,
    while a metadata endpoint that has not answered in seconds is not going
    to.
    """
    warmed = await warm_up(base_url, model, fetch=warmup_fetch or fetch) if warmup else False
    probe = await probe_serving(base_url, model, fetch=fetch)
    return serving_metadata(model=model, probe=probe, warmup=warmed)


def warn_if_unpinned(serving: dict[str, Any]) -> None:
    """Say so on stderr when the run cannot be published.

    A publishable row needs an empty `unavailable` list; an operator who
    only sees the markdown report would otherwise not learn that until the
    artifact was already on the scoreboard.
    """
    missing = serving.get("unavailable") or []
    if missing:
        print(
            f"warning: serving environment not fully pinned: {', '.join(missing)}",
            file=sys.stderr,
        )


async def warm_up(base_url: str, model: str, *, fetch: Fetch) -> bool:
    """Load the model before the first scored scenario; report whether it worked.

    Without this the first scenario absorbs however long the weights take to
    page in, which is not a property of the model's reasoning. Returns
    `False` when the request failed so the artifact never claims a warm-up
    that did not happen.
    """
    try:
        await fetch(f"{ollama_root(base_url)}/api/generate", {"model": model})
    except Exception:
        return False
    return True


def report_payload(reports: list[ScenarioReport]) -> list[dict[str, Any]]:
    """JSON-serializable form of the reports, for machine consumption.

    `successes` counts repetitions whose **diagnosis** was graded correct
    (`grade.diagnosis_success`), which is the historical scoreboard number
    and is deliberately narrower than the run's `outcome`: a run can
    diagnose correctly and still be published as a failure because it
    missed its evidence, errored, or violated the write boundary.

    The journey artifact does not reuse this key. A conversation has no
    single diagnosis, so it publishes `successful_journeys` — repetitions
    in which every turn's outcome was `success` — rather than two
    different measurements under one name.
    """
    return [
        {
            "scenario": report.scenario_id,
            "root_cause": report.root_cause,
            "successes": report.successes,
            "evidence_hits": report.evidence_hits,
            # The screen the question was asked from. A diagnostic score
            # without it is not reproducible: the same question against a
            # different starting pane is a different measurement.
            "interaction": (
                None if report.interaction is None else interaction_payload(report.interaction)
            ),
            "max_tool_calls": report.max_tool_calls,
            "runs": [dataclasses.asdict(run) for run in report.runs],
        }
        for report in reports
    ]


def policy_payload(policy: ResolvedAgentPolicy) -> dict[str, Any]:
    """Which model, which tier, and who decided the tier."""
    return {
        "provider": policy.model.provider,
        "model": policy.model.model,
        "tier": policy.tier.value,
        "route_source": policy.route_source.value,
        "prompt_pack": tier_prompt_id(policy),
    }


def limits_payload(policy: ResolvedAgentPolicy) -> dict[str, Any]:
    """Every budget the run was bound by.

    Published in full because they are not implied by the tier for a
    reader outside this repository, and a tier's budgets can change
    between releases.
    """
    return {
        "max_iterations": policy.max_iterations,
        "max_history_chars": policy.max_history_chars,
        "max_result_chars": policy.max_result_chars,
        "max_tool_calls_per_iteration": policy.max_tool_calls_per_iteration,
        "allow_parallel_tool_calls": policy.allow_parallel_tool_calls,
        "strict_history_budget": policy.strict_history_budget,
    }


def capabilities_payload(policy: ResolvedAgentPolicy) -> dict[str, Any]:
    """The merged capability facts, each with the source that supplied it.

    Provenance matters more than the values: a tier routed from a catalog
    entry and one routed from a provider's own claim are different
    evidence for the same number.
    """
    capabilities = policy.capabilities
    tier = capabilities.recommended_tier
    return {
        "context_window_tokens": capabilities.context_window_tokens,
        "supports_tools": capabilities.supports_tools,
        "supports_parallel_tools": capabilities.supports_parallel_tools,
        "supports_reasoning": capabilities.supports_reasoning,
        "recommended_tier": None if tier is None else tier.value,
        "provenance": {
            fact: source.value for fact, source in sorted(capabilities.provenance.items())
        },
    }


def tools_payload(policy: ResolvedAgentPolicy, omitted: list[str]) -> dict[str, Any]:
    """The exact armed names, their count, and what a controlled arm dropped."""
    armed = list(armed_tool_names(policy))
    return {"armed": armed, "count": len(armed), "omitted": omitted}


def prompt_fingerprint(
    policy: ResolvedAgentPolicy,
    *,
    grind: PromptGrind = NO_GRIND,
) -> dict[str, Any]:
    """Which prompt produced a run.

    A scoreboard row that does not say which prompt it was measured under
    is not comparable with any other row, so every run records this.

    The digest covers what the model actually receives: the *composed*
    system message for this policy — the immutable safety contract, the
    common role, the tier pack, any overlay, and the armed-capability
    clauses — plus the complete tool schemas, which are retransmitted on
    every request. Hashing only the tier pack would mark behaviourally
    different runs as comparable.

    Args:
        policy: The resolved policy used by the run.
        grind: The eval-only prompt levers this run applied.

    Returns:
        The `pack` id, the composed `overlays`, `source` (`default` or
        `override`) and the `sha256` digest. `source` reflects the
        *effect* of the grind: text that reproduces korvid's own wording
        byte for byte still yields a comparable, publishable run.
    """
    digest = _prompt_digest(static_prompt(policy, grind), policy)
    baseline = _prompt_digest(static_prompt(policy), policy)
    return {
        "pack": tier_prompt_id(policy),
        "overlays": list(grind_layer_ids(grind)),
        "source": "default" if digest == baseline else "override",
        "sha256": digest,
    }


def _prompt_digest(system_prompt: str, policy: ResolvedAgentPolicy) -> str:
    schemas = [_plain(tool) for tool in policy.tools]
    digest = hashlib.sha256()
    digest.update(system_prompt.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(schemas, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _plain(value: Any) -> Any:
    """Deep-copy a frozen schema into plain JSON-serializable containers."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def run_payload(
    reports: list[ScenarioReport],
    *,
    policy: ResolvedAgentPolicy,
    grind: PromptGrind = NO_GRIND,
    serving: dict[str, Any] | None = None,
    omitted_tools: list[str] | None = None,
) -> dict[str, Any]:
    """The full JSON artifact: run metadata plus per-scenario results.

    `serving` is omitted when it was not captured, so an artifact written
    before #235 stays distinguishable from one whose probe returned
    nothing.
    """
    # De-duplicated: the flag is repeatable, and naming a tool twice still
    # removed one tool.
    omitted = sorted(set(omitted_tools or []))
    meta: dict[str, Any] = {
        "policy": policy_payload(policy),
        "limits": limits_payload(policy),
        "capabilities": capabilities_payload(policy),
        "catalog_version": policy.catalog_version,
        "prompts": prompt_fingerprint(policy, grind=grind),
        # Named, not left to be inferred from the digest: recovering the
        # arm from a hash means keeping a lookup table outside the artifact.
        "tools": tools_payload(policy, omitted),
    }
    if serving is not None:
        meta["serving"] = serving
    return {"meta": meta, "scenarios": report_payload(reports)}


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m korvid.evals",
        description="Run the agent eval scenario pack against a live model.",
    )
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=bundled_scenarios_dir(),
        help="directory of scenario YAML files (default: bundled pack)",
    )
    parser.add_argument(
        "--reps",
        type=_positive_int,
        default=DEFAULT_REPETITIONS,
        help=f"repetitions per scenario, at least 1 (default: {DEFAULT_REPETITIONS})",
    )
    parser.add_argument(
        "--model-tier",
        choices=("low", "high"),
        default=None,
        help=(
            "evaluate this capability tier; omit to let the shipped model "
            "catalog route the model exactly as the TUI does"
        ),
    )
    parser.add_argument(
        "--tier-pack-file",
        type=Path,
        default=None,
        help=(
            "replace the tier's operating pack with this file's contents. "
            "Eval-only prompt grinding: it is layered after korvid's "
            "immutable safety contract and can never widen it. The result "
            "JSON records the override so the run is not mistaken for a "
            "default-prompt score"
        ),
    )
    parser.add_argument(
        "--prompt-overlay-file",
        type=Path,
        default=None,
        help=(
            "layer this file's contents on top of the tier pack as an "
            "eval overlay, published as 'eval-overlay'"
        ),
    )
    parser.add_argument(
        "--without-tool",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "drop a tool from the measured surface, repeatable; for the "
            "controlled arms of issue #221. An unknown name is refused, "
            "because a typo would silently measure the full surface"
        ),
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help=(
            "send one throwaway request before the first scored scenario so "
            "model load time does not land in it; recorded in the result JSON"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also write the markdown report to this file",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write per-run metrics as JSON to this file",
    )
    args = parser.parse_args(argv)
    _validate_tool_names(args.without_tool, args.model_tier)
    return args


def _validate_tool_names(names: list[str], model_tier: str | None) -> None:
    """Refuse a name this run's tier does not actually arm.

    Checked against the resolved surface, not the whole registry: a write
    tool is never armed in an eval (the environment is read-only) and a
    high-tier navigation tool is not on the low surface, so naming either
    would drop nothing while `meta.tools.omitted` claimed it did — an arm
    published as reduced that is byte-identical to the full one. The same
    reasoning covers a plain typo.

    An omitted `--model-tier` is checked against the low surface, which is
    what automatic routing selects for every catalogued model today; name
    the tier explicitly to reduce a high-tier-only tool.
    """
    known = eval_surface_names(model_tier)
    unknown = sorted(set(names) - known)
    if unknown:
        tier = model_tier or "low"
        raise SystemExit(
            f"--without-tool: {', '.join(unknown)} not on the measured surface;"
            f" the {tier} tier arms {', '.join(sorted(known))}"
        )


#: Metadata probes must not hold the campaign: an endpoint that has not
#: answered `/api/version` in this long is not going to.
PROBE_TIMEOUT_SECONDS = 20.0

#: The warm-up is a real model load, which for a 30B off cold storage is
#: minutes rather than seconds.
WARMUP_TIMEOUT_SECONDS = 900.0


def _executor_factory(scenario: Scenario) -> Callable[[], ToolExecutor]:
    def factory() -> ToolExecutor:
        return ToolExecutor(FakeKubeClient(scenario), builtin_aliases())

    return factory


async def _run_all(
    scenarios: list[Scenario],
    provider_factory: Callable[[], Any],
    repetitions: int,
    policy: ResolvedAgentPolicy,
    grind: PromptGrind,
) -> list[ScenarioReport]:
    reports: list[ScenarioReport] = []
    for scenario in scenarios:
        print(f"running {scenario.id} x{repetitions} ...", file=sys.stderr)
        reports.append(
            await run_scenario(
                scenario,
                provider_factory=provider_factory,
                executor_factory=_executor_factory(scenario),
                repetitions=repetitions,
                policy=policy,
                grind=grind,
            )
        )
    return reports


def exit_code(reports: list[ScenarioReport]) -> int:
    """Nonzero when any run errored — an unreachable or misconfigured
    endpoint must not look like a completed evaluation to calling scripts.
    The underlying reasons go to stderr because the markdown report only
    carries aggregate counts."""
    errored = 0
    for report in reports:
        for run in report.runs:
            if run.error is not None:
                errored += 1
                print(f"error: {report.scenario_id}: {run.error}", file=sys.stderr)
    if errored:
        print(f"{errored} run(s) errored.", file=sys.stderr)
        return 1
    return 0


def _prompt_grind(args: argparse.Namespace) -> PromptGrind:
    """The eval-only prompt levers, read from the CLI's file flags."""
    return PromptGrind(
        tier_pack=_read_prompt_file(args.tier_pack_file, "--tier-pack-file"),
        overlay=_read_prompt_file(args.prompt_overlay_file, "--prompt-overlay-file"),
    )


def _resolve_policy(
    provider_factory: Callable[[], Any],
    args: argparse.Namespace,
) -> ResolvedAgentPolicy:
    """Route once for the whole campaign."""
    provider = provider_factory()
    try:
        policy = resolve_eval_policy(
            provider,
            model_tier=args.model_tier,
            omit_tools=frozenset(args.without_tool),
        )
    except UnknownEvalToolError as exc:
        raise SystemExit(f"--without-tool: {exc}") from exc
    finally:
        aclose = getattr(provider, "aclose", None)
        if callable(aclose):
            asyncio.run(aclose())
    return policy


def _read_prompt_file(path: Path | None, flag: str) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"{flag}: cannot read {path}: {exc.strerror}") from exc
    except UnicodeError as exc:
        # read_text raises UnicodeDecodeError, which is not an OSError; a
        # non-UTF-8 file must still get the actionable message, not a
        # traceback.
        raise SystemExit(f"{flag}: {path} is not valid UTF-8: {exc}") from exc
    if not text:
        raise SystemExit(f"{flag}: {path} is empty")
    return text


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    args = _parse_args(argv)
    provider_factory = provider_factory_from_env(os.environ)
    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        raise SystemExit(f"no scenario YAML files found in {args.scenarios}")
    grind = _prompt_grind(args)
    policy = _resolve_policy(provider_factory, args)
    serving = asyncio.run(
        capture_serving(
            os.environ.get("KORVID_EVAL_BASE_URL", "").strip(),
            eval_model_tag(os.environ),
            fetch=httpx_fetch(
                api_key=eval_api_key(os.environ),
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
            ),
            warmup_fetch=httpx_fetch(
                api_key=eval_api_key(os.environ),
                timeout_seconds=WARMUP_TIMEOUT_SECONDS,
            ),
            warmup=args.warmup,
        )
    )
    if serving["unavailable"]:
        print(
            f"warning: serving environment not fully pinned: {', '.join(serving['unavailable'])}",
            file=sys.stderr,
        )
    reports = asyncio.run(_run_all(scenarios, provider_factory, args.reps, policy, grind))
    markdown = render_markdown(reports)
    print(markdown)
    if args.out is not None:
        args.out.write_text(markdown + "\n")
    if args.json is not None:
        payload = run_payload(
            reports,
            policy=policy,
            grind=grind,
            serving=serving,
            omitted_tools=args.without_tool,
        )
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return exit_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
