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
import copy
import dataclasses
import hashlib
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from korvid.agent.profiles import AgentProfile, PromptOverrides, build_profile
from korvid.agent.prompts import compose_system_prompt
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.runner import (
    DEFAULT_REPETITIONS,
    ScenarioReport,
    _eval_tools,
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
    """JSON-serializable form of the reports, for machine consumption."""
    return [
        {
            "scenario": report.scenario_id,
            "root_cause": report.root_cause,
            "successes": report.successes,
            "evidence_hits": report.evidence_hits,
            "runs": [dataclasses.asdict(run) for run in report.runs],
        }
        for report in reports
    ]


def prompt_fingerprint(
    profile: AgentProfile,
    *,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Which prompt produced a run.

    A scoreboard row that does not say which prompt it was measured under is
    not comparable with any other row, so every run records this.

    The digest covers what the model actually receives: the *composed*
    system prompt for the surface in question — role statement plus the UI
    and write/no-write clauses `AgentRuntime` appends — and the complete
    tool schemas, which are retransmitted on every request. Hashing only
    the role statement would mark behaviourally different runs as
    comparable.

    Args:
        profile: the built profile, already carrying any overrides. It is
            the only input: `source` is decided by comparing this profile
            against the shipped prompts, not by inspecting what was
            configured, so an override that reproduces korvid's own wording
            is correctly reported as `default`.
        tools: the schemas actually offered. Defaults to the task pack's
            surface (`_eval_tools`, which drops the UI tools); journey runs
            offer `profile.tools` unchanged and must pass them, or a UI
            schema change would leave the digest untouched.

    Returns:
        `source` (`default` or `override`) and the `sha256` digest.
        `source` reflects the *effect* of the configuration: an override
        that reproduces the shipped prompt byte for byte still yields a
        comparable, publishable run.
    """
    offered = _eval_tools(profile) if tools is None else tools
    digest = _prompt_digest(profile.system_prompt, profile.ui_prompt, offered)
    return {"source": _source(profile, offered, digest), "sha256": digest}


def _source(profile: AgentProfile, offered: list[dict[str, Any]], digest: str) -> str:
    """`default` when the configuration had no effect on what the model sees.

    Compared against the shipped prompts **on the same tool set**, so the
    answer does not depend on which tools this cluster happened to arm. An
    override that reproduces korvid's own wording byte for byte still
    yields a comparable, publishable run.
    """
    shipped = build_profile(
        profile.name, readonly=False, resize_supported=True, overrides=PromptOverrides()
    )
    descriptions = {t["function"]["name"]: t["function"]["description"] for t in shipped.tools}
    baseline_tools = copy.deepcopy(offered)
    for tool in baseline_tools:
        function = tool["function"]
        shipped_description = descriptions.get(function["name"])
        if shipped_description is not None:
            function["description"] = shipped_description
    baseline = _prompt_digest(shipped.system_prompt, shipped.ui_prompt, baseline_tools)
    return "default" if digest == baseline else "override"


def _prompt_digest(system_prompt: str, ui_prompt: str, tools: list[dict[str, Any]]) -> str:
    composed = compose_system_prompt(tools, None, system_prompt=system_prompt, ui_prompt=ui_prompt)
    digest = hashlib.sha256()
    digest.update(composed.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def run_payload(
    reports: list[ScenarioReport],
    *,
    profile: AgentProfile,
    overrides: PromptOverrides,
    serving: dict[str, Any] | None = None,
    omitted_tools: list[str] | None = None,
) -> dict[str, Any]:
    """The full JSON artifact: run metadata plus per-scenario results.

    `serving` is omitted when it was not captured, so an artifact written
    before #235 stays distinguishable from one whose probe returned
    nothing.
    """
    omitted = sorted(omitted_tools or [])
    offered = _eval_tools(profile, frozenset(omitted))
    meta: dict[str, Any] = {
        "profile": profile.name,
        "prompts": prompt_fingerprint(profile, tools=offered),
        # Named, not left to be inferred from the digest: recovering the arm
        # from a hash means keeping a lookup table outside the artifact.
        "tools": {"omitted": omitted, "count": len(offered)},
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
        "--profile",
        choices=("full", "small"),
        default="full",
        help="agent capability profile to evaluate (issue #71; default: full)",
    )
    parser.add_argument(
        "--system-prompt-file",
        type=Path,
        default=None,
        help=(
            "replace the profile's role statement with this file's contents; "
            "the result JSON records the override so the run is not mistaken "
            "for a default-prompt score"
        ),
    )
    parser.add_argument(
        "--prompt-append-file",
        type=Path,
        default=None,
        help="append this file's contents to the profile's role statement",
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
    _validate_tool_names(args.without_tool, args.profile)
    return args


def _validate_tool_names(names: list[str], profile_name: str) -> None:
    """Refuse a name that is not on the surface this run actually offers.

    Checked against `_eval_tools`, not `profile.tools`: the UI tools are
    already excluded from every eval run, so naming one would drop nothing
    while `meta.tools.omitted` claimed it did - an arm published as reduced
    that is byte-identical to the full one. The same reasoning covers a
    plain typo, which would measure the full surface under the reduced
    arm's name.
    """
    profile = build_profile(
        profile_name, readonly=False, resize_supported=True, overrides=PromptOverrides()
    )
    known = {tool["function"]["name"] for tool in _eval_tools(profile)}
    unknown = sorted(set(names) - known)
    if unknown:
        raise SystemExit(
            f"--without-tool: {', '.join(unknown)} not on the measured surface;"
            f" the {profile_name} profile offers {', '.join(sorted(known))}"
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
    profile: str,
    overrides: PromptOverrides,
    omit_tools: frozenset[str] = frozenset(),
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
                profile=profile,
                overrides=overrides,
                omit_tools=omit_tools,
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


def _sweep_overrides(args: argparse.Namespace) -> PromptOverrides:
    """Prompt overrides for a sweep run, read from the CLI's file flags."""
    return PromptOverrides(
        system=_read_prompt_file(args.system_prompt_file, "--system-prompt-file"),
        append=_read_prompt_file(args.prompt_append_file, "--prompt-append-file"),
    )


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
    overrides = _sweep_overrides(args)
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
    reports = asyncio.run(
        _run_all(
            scenarios,
            provider_factory,
            args.reps,
            args.profile,
            overrides,
            frozenset(args.without_tool),
        )
    )
    markdown = render_markdown(reports)
    print(markdown)
    if args.out is not None:
        args.out.write_text(markdown + "\n")
    if args.json is not None:
        # The profile is rebuilt here purely to fingerprint the run; the
        # scenarios above each built their own from the same inputs.
        profile = build_profile(
            args.profile, readonly=False, resize_supported=True, overrides=overrides
        )
        payload = run_payload(
            reports,
            profile=profile,
            overrides=overrides,
            serving=serving,
            omitted_tools=args.without_tool,
        )
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return exit_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
