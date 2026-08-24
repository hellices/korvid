"""Live eval CLI: `python -m korvid.evals` (issue #69).

Runs the bundled (or a custom) scenario pack against a live
OpenAI-compatible endpoint and prints a markdown report. This is a
manual, on-demand tool — it talks to a real model and is never part of
CI (CI covers the harness itself with scripted-provider smoke tests).

Configuration comes from the environment:

- `KORVID_EVAL_BASE_URL` — OpenAI-compatible endpoint base URL (required)
- `KORVID_EVAL_MODEL` — model name (required)
- `KORVID_EVAL_API_KEY` — bearer token, if the endpoint needs one
- `KORVID_EVAL_TIMEOUT_SECONDS` — read timeout for slow local models (default 60)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.harness import (
    NO_GRIND,
    PromptGrind,
    UnknownEvalToolError,
    armed_tool_names,
    baseline_eval_policy,
    eval_surface_names,
    ground_eval_policy,
    resolve_eval_policy,
    static_prompt,
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
from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.static_creds import StaticHeaderSource
from korvid.tools.executor import ToolExecutor


def provider_factory_from_env(env: Mapping[str, str]) -> Callable[[], Any]:
    """Build a live-provider factory from `KORVID_EVAL_*` variables."""
    base_url = env.get("KORVID_EVAL_BASE_URL", "").strip()
    model = env.get("KORVID_EVAL_MODEL", "").strip()
    if not base_url or not model:
        raise SystemExit(
            "korvid.evals needs a live model endpoint: set KORVID_EVAL_BASE_URL"
            " and KORVID_EVAL_MODEL (and KORVID_EVAL_API_KEY if required)."
        )
    api_key = env.get("KORVID_EVAL_API_KEY", "").strip()
    raw_timeout = env.get("KORVID_EVAL_TIMEOUT_SECONDS", "60").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        timeout_seconds = 0
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SystemExit("KORVID_EVAL_TIMEOUT_SECONDS must be a positive number.")

    def factory() -> OpenAICompatProvider:
        credentials = StaticHeaderSource(api_key) if api_key else None
        return OpenAICompatProvider(
            base_url,
            model,
            credentials=credentials,
            timeout_seconds=timeout_seconds,
        )

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
        "prompt_pack": policy.prompt_pack_id,
        "overlays": list(policy.prompt_overlay_ids),
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
        policy: The resolved policy, exactly as the run was composed
            against it. A campaign passes its already-ground policy, so
            `overlays` here is the same list `meta.policy` publishes.
        grind: The eval-only prompt levers this run applied.

    Returns:
        The `pack` id, the composed `overlays`, `source` (`default` or
        `override`) and the `sha256` digest. `source` reflects the
        *effect* of the grind: text that reproduces korvid's own wording
        byte for byte still yields a comparable, publishable run.
    """
    ground = ground_eval_policy(policy, grind)
    # The baseline is korvid's own wording, which the shipped registry can
    # only compose for a policy that does not name the eval overlay.
    baseline_policy = baseline_eval_policy(policy)
    digest = _prompt_digest(static_prompt(ground, grind), ground)
    baseline = _prompt_digest(static_prompt(baseline_policy), baseline_policy)
    return {
        "pack": ground.prompt_pack_id,
        "overlays": list(ground.prompt_overlay_ids),
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
    grind: PromptGrind = NO_GRIND,
) -> ResolvedAgentPolicy:
    """Route and ground once, for the whole campaign.

    Every repetition of every scenario is composed against this one
    policy, so the artifact's `meta.policy` describes the run rather than
    whichever repetition happened to be inspected. The grind's overlay id
    is applied here too: the session, `meta.policy`, `meta.prompts` and
    the report then all read the same object, and grounding being
    idempotent keeps the harness from naming the overlay twice.
    """
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
    return ground_eval_policy(policy, grind)


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
    policy = _resolve_policy(provider_factory, args, grind)
    serving = asyncio.run(
        capture_serving(
            os.environ.get("KORVID_EVAL_BASE_URL", "").strip(),
            os.environ.get("KORVID_EVAL_MODEL", "").strip(),
            fetch=httpx_fetch(
                api_key=os.environ.get("KORVID_EVAL_API_KEY", "").strip(),
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
            ),
            warmup_fetch=httpx_fetch(
                api_key=os.environ.get("KORVID_EVAL_API_KEY", "").strip(),
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
