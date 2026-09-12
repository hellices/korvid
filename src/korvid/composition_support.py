"""Data, adapters, and lifecycle helpers used by the composition root."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib.util
import logging
import os
import ssl
import threading
from collections.abc import Callable, Collection, Iterator
from typing import TYPE_CHECKING, Any, Final, Protocol

from korvid.agent.install_hint import isolated_install_hint
from korvid.agent.interaction import (
    AgentUiBridge,
    ClusterFacts,
    InteractionContext,
    UiAction,
    UiActionResult,
)
from korvid.core.config import KorvidConfig, ModelConnectionConfig, context_is_protected
from korvid.core.mcp import MCPControllerBase
from korvid.k8s.client import KubeClient
from korvid.k8s.csp import ProviderInfo
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import PACKAGES_GROUP
from korvid.tools.executor import UIBridge
from korvid.tools.structured import ERROR_PREFIX

if TYPE_CHECKING:
    from korvid.agent.provider import LLMProvider
    from korvid.agent.session import AgentSession
    from korvid.ui.app import KorvidApp

logger = logging.getLogger("korvid.__main__")

_CLEANUP_GRACE_SECONDS = 5.0
_CLEANUP_CANCEL_SECONDS = 1.0
_MCP_SHUTDOWN_GRACE_SECONDS = 11.0
_RUNNER_SHUTDOWN_SECONDS = 5.0

_AGENT_INSTALL_HINT = (
    "the embedded agent is enabled (agent.active names a profile in config.yaml) but its "
    f"dependencies are not installed — {isolated_install_hint(feature='agent')}"
)
_PROMPT_DEGRADE_HINT: Final[str] = (
    "shorten agent.rules or route to a larger-context model with `:ai`"
)
_UNKNOWN_CLUSTER = ClusterFacts(provider="unknown", distribution=None)


def _missing_extra_packages(extra_roots: frozenset[str]) -> list[str]:
    """Return missing top-level packages for one optional extra."""
    return sorted(pkg for pkg in extra_roots if importlib.util.find_spec(pkg) is None)


@dataclasses.dataclass(frozen=True, slots=True)
class ObservabilityWiring:
    """The optional observability connectors owned by one session."""

    metrics: Any = None
    logs: Any = None

    @property
    def backends(self) -> frozenset[str]:
        """Return the names of configured backends."""
        names: set[str] = set()
        if self.metrics is not None:
            names.add("metrics")
        if self.logs is not None:
            names.add("logs")
        return frozenset(names)

    async def aclose(self) -> None:
        """Close all owned clients, attempting logs after metrics failures."""
        try:
            if self.metrics is not None:
                await self.metrics.aclose()
        finally:
            if self.logs is not None:
                await self.logs.aclose()


def _custom_column_names(config: KorvidConfig) -> dict[str, tuple[str, ...]]:
    """Return configured custom-column names by resource alias."""
    return {
        kind: tuple(column.name for column in view.columns) for kind, view in config.views.items()
    }


class _MCPAppHooks:
    """Late-bound application hooks for MCP follow mode."""

    def __init__(self) -> None:
        self.app: KorvidApp | None = None

    def follow_enabled(self) -> bool:
        """Report whether the bound app has follow mode enabled."""
        return self.app is not None and self.app.integrations.follow_enabled

    def note_activity(self, line: str) -> None:
        """Forward a mirrored activity line when an app is bound."""
        if self.app is not None:
            self.app.integrations.note_activity(line)


def _force_runner_exit() -> None:
    """Exit without blocking I/O or logging locks on the watchdog thread."""
    os._exit(1)


async def _shutdown(
    discovery_task: asyncio.Task[None] | None,
    provider: LLMProvider | None,
    kube: KubeClient,
    *,
    session: AgentSession | None = None,
    close_tasks: set[asyncio.Future[Any]] | None = None,
) -> None:
    """Drain agent cleanup without letting it hold the Kubernetes client hostage.

    A standalone caller owns the terminal policy; `_teardown` instead supplies
    its task set so observability clients are also attempted before that policy.
    """
    tasks = close_tasks if close_tasks is not None else set()
    if discovery_task is not None:
        discovery_task.cancel()
        _track_cleanup_task(discovery_task, tasks, "resource discovery task")
    if session is not None or provider is not None:
        _close_agent_in_background(session, provider, tasks)
    try:
        await _drain_cleanup_tasks(tasks)
    finally:
        kube_task = asyncio.create_task(kube.close())
        _track_cleanup_task(kube_task, tasks, "Kubernetes client close")
        try:
            await _drain_cleanup_tasks({kube_task})
        finally:
            if close_tasks is None:
                _exit_if_cleanup_pending(tasks)


def _track_cleanup_task(
    task: asyncio.Future[Any] | None, tasks: set[asyncio.Future[Any]], label: str
) -> None:
    """Retain owned work and consume failures without logging plugin payloads."""
    if task is None or task in tasks:
        return
    tasks.add(task)

    def reap(finished: asyncio.Future[Any]) -> None:
        tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.warning("%s failed during cleanup", label)

    task.add_done_callback(reap)


def _log_cleanup_loop_error(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Consume loop-reported failures without formatting untrusted context."""
    future = context.get("future", context.get("task"))
    if isinstance(future, asyncio.Future) and future.done() and not future.cancelled():
        future.exception()
    logger.warning("Event loop operation failed")


@contextlib.contextmanager
def _own_run_tasks() -> Iterator[None]:
    """Own children at creation, including shielded tasks that finish before teardown.

    Retain descendants locally, outside the explicit cleanup grace set. The
    final sweep adopts remaining descendants only after clients have closed.
    The composition root owns this loop for one run. Delegate existing factory
    keywords unchanged, and restore both hooks for callers using an ambient loop.
    The exception handler also sanitizes explicit loop reports from shield and
    async-generator cleanup, which can occur even after a result was retrieved.
    """
    tasks: set[asyncio.Future[Any]] = set()
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    previous_handler = loop.get_exception_handler()

    def create_task(
        owner_loop: asyncio.AbstractEventLoop, coroutine: Any, **kwargs: Any
    ) -> asyncio.Future[Any]:
        task = (
            previous_factory(owner_loop, coroutine, **kwargs)
            if previous_factory is not None
            else asyncio.Task(coroutine, loop=owner_loop, **kwargs)
        )
        _track_cleanup_task(task, tasks, "run background task")
        return task

    loop.set_task_factory(create_task)
    loop.set_exception_handler(_log_cleanup_loop_error)
    try:
        yield
    finally:
        loop.set_task_factory(previous_factory)
        loop.set_exception_handler(previous_handler)


async def _drain_cleanup_tasks(
    tasks: Collection[asyncio.Future[Any]], *, timeout: float | None = None
) -> None:
    """Wait once for grace, then once for cancellation, never joining indefinitely.

    Unlike `wait_for`, `wait` does not wait out a task that suppresses
    cancellation. Such tasks remain owned for the final terminal decision.
    Caller cancellation still cancels the owned work before propagating.
    """
    pending = set(tasks)
    if not pending:
        return
    try:
        _, pending = await asyncio.wait(
            pending, timeout=_CLEANUP_GRACE_SECONDS if timeout is None else timeout
        )
    finally:
        pending = {task for task in pending if not task.done()}
        if pending:
            logger.warning("Cleanup incomplete; cancelling pending tasks")
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=_CLEANUP_CANCEL_SECONDS)


def _exit_if_cleanup_pending(tasks: Collection[asyncio.Future[Any]]) -> None:
    """Fail terminally after client cleanup if an owned task will not stop.

    Python cannot forcibly stop a cancellation-resistant task. Returning or
    raising SystemExit would leave `Runner.close` to try cancelling it again.
    A nonzero process exit deliberately skips finalization and crash recovery,
    forfeiting remaining task finalizers only after the client cleanup budgets.
    A separate watchdog also bounds blocked terminal diagnostics before the
    runner begins finalization.
    """
    if any(not task.done() for task in tasks):
        watchdog: threading.Timer | None = None
        try:
            watchdog = threading.Timer(_RUNNER_SHUTDOWN_SECONDS, _force_runner_exit)
            watchdog.daemon = True
            watchdog.start()
            logger.critical(
                "Cleanup tasks did not stop after cancellation; exiting without restart"
            )
        finally:
            try:
                os._exit(1)
            finally:
                if watchdog is not None:
                    watchdog.cancel()


async def _discover_in_background(
    kube: KubeClient, aliases: dict[str, ResourceMeta], app: KorvidApp
) -> None:
    """Merge full API discovery into *aliases* once available (shared dict)."""
    try:
        metas = await kube.discover_resources()
        discovered = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META, *metas])
    except Exception:
        logger.warning("Resource discovery failed; staying pods-only", exc_info=True)
        return
    aliases.update(discovered)
    # Where OLM serves the operator catalog, `:operators` opens it - unless a
    # real kind (e.g. OLM v1's Operator) already claims that alias.
    pkg_meta = aliases.get(f"packagemanifests.{PACKAGES_GROUP}")
    if pkg_meta is not None:
        aliases.setdefault("operators", pkg_meta)
    app.on_aliases_updated()


def _close_provider_in_background(provider: LLMProvider, tasks: set[asyncio.Future[Any]]) -> None:
    """Close an old provider without blocking, keeping a strong task reference.

    asyncio only holds weak references to tasks, so fire-and-forget tasks can
    be garbage-collected before completion; the done callback also consumes
    any close error to avoid 'Task exception was never retrieved' warnings.
    """
    task = asyncio.get_running_loop().create_task(provider.aclose())
    _track_cleanup_task(task, tasks, "provider close")


class _AgentToolUIBridgeProxy(UIBridge):
    """Late-bound *tools-layer* UI bridge: the ToolExecutor is built before the app exists,
    so it holds this proxy and the composition root points ``target`` at the
    app's bridge adapter right after construction. Until then every UI tool
    degrades to an ERROR result instead of crashing the turn.

    All delegated calls are serialized through one lock: the built-in agent
    and the MCP server's concurrent stateless requests share this proxy, and
    the app's UI operations (log pane swaps, describe views) are not safe to
    interleave - only navigation has its own lock inside the app."""

    #: Composed from the product's own error prefix rather than spelled out:
    #: every caller decides "this failed" by testing `ERROR_PREFIX`, so a
    #: literal here would quietly demote this answer to an ordinary text
    #: result if that constant ever changed.
    _NOT_READY = f"{ERROR_PREFIX} UI not ready"

    def __init__(self) -> None:
        self.target: UIBridge | None = None
        self._lock = asyncio.Lock()

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_navigate(view, namespace)

    async def agent_set_filter(self, pattern: str) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_set_filter(pattern)

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_open_logs(pod, namespace, container)

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_open_describe(kind, name, namespace)

    async def agent_drill_down(self, name: str) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_drill_down(name)

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_request_write(
                action, kind, name, namespace, replicas, resources
            )

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_submit_write_proposal(
                action,
                kind,
                name,
                namespace,
                replicas,
                resources,
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_get_write_proposal(proposal_id)

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_cancel_write_proposal(proposal_id, session_id=session_id)


def _cluster_facts(info: ProviderInfo) -> ClusterFacts:
    """Convert a cloud-provider probe to agent-facing facts."""
    return ClusterFacts(provider=info.provider, distribution=info.distribution)


@dataclasses.dataclass(frozen=True)
class AgentWiring:
    """The agent resources held by the app and teardown guard."""

    session: AgentSession | None
    available: bool
    rebuild: Callable[[ModelConnectionConfig, str | None], AgentSession | None] | None
    retarget: Callable[[AgentSession | None, bool, ClusterFacts | None], None]
    disconnect: Callable[[], None]
    provider_box: list[LLMProvider | None]
    session_box: list[AgentSession | None]
    tool_bridge: _AgentToolUIBridgeProxy
    ui_bridge: _AgentUiBridgeProxy


class _AgentUiBridgeProxy(AgentUiBridge):
    """Late-bound agent-layer workspace bridge."""

    _NOT_READY = "agent UI not ready"

    def __init__(self) -> None:
        self.target: AgentUiBridge | None = None

    def snapshot(self) -> InteractionContext:
        if self.target is None:
            raise RuntimeError(self._NOT_READY)
        return self.target.snapshot()

    async def apply(self, action: UiAction) -> UiActionResult:
        if self.target is None:
            raise RuntimeError(self._NOT_READY)
        return await self.target.apply(action)


def _agent_unavailable_wiring(
    config: KorvidConfig,
    missing: list[str],
    ui_proxy: _AgentToolUIBridgeProxy,
    agent_ui_proxy: _AgentUiBridgeProxy,
    provider_box: list[LLMProvider | None],
    session_box: list[AgentSession | None],
) -> AgentWiring:
    """Build session-less wiring when the agent extra is unavailable."""
    if config.agent_enabled:
        raise SystemExit(f"korvid: {_AGENT_INSTALL_HINT}")
    logger.info(
        "embedded-agent providers not installed; :ai disabled (missing %s)", ", ".join(missing)
    )

    def _retarget_noop(
        session: AgentSession | None,
        pod_resize_supported: bool,
        cluster: ClusterFacts | None,
    ) -> None:
        return None

    return AgentWiring(
        session=None,
        available=False,
        rebuild=None,
        retarget=_retarget_noop,
        disconnect=lambda: None,
        provider_box=provider_box,
        session_box=session_box,
        tool_bridge=ui_proxy,
        ui_bridge=agent_ui_proxy,
    )


def _agent_environment(
    config: KorvidConfig,
    pod_resize_supported: bool,
    observability_backends: frozenset[str],
) -> Any:
    """Build the capability facts used for model-policy routing."""
    from korvid.agent.model_policy import PolicyEnvironment

    return PolicyEnvironment(
        readonly=config.readonly,
        resize_supported=pod_resize_supported,
        observability_backends=observability_backends,
    )


def _active_model_name(config: KorvidConfig) -> str | None:
    """Return the active profile's bare model name for the initial header."""
    from korvid.agent.model_profiles import split_reference

    profile = config.model_connections.active_profile
    if profile is None:
        return None
    return split_reference(profile.model)[1] or None


def _warn_agent_disabled(error: Exception, startup_warnings: list[str] | None) -> None:
    """Record a safe, actionable warning for a refused agent session."""
    from korvid.agent.prompt_harness import StaticPromptTooLargeError

    detail = str(error)
    if isinstance(error, StaticPromptTooLargeError):
        detail = f"{detail} — {_PROMPT_DEGRADE_HINT}"
        logger.warning("agent session not built; the system prompt does not fit the routed model")
    else:
        logger.warning("agent session not built: %s", type(error).__name__)
    if startup_warnings is not None:
        startup_warnings.append(f"agent disabled: {detail}")


def _close_agent_in_background(
    session: AgentSession | None,
    provider: LLMProvider | None,
    tasks: set[asyncio.Future[Any]],
) -> None:
    """Release a replaced session and its provider, in that order.

    The session first: it may still be winding a turn down, and closing
    the transport under it would turn an orderly stop into a torn stream.
    Non-blocking, because a swap must not stall the UI on a provider that
    is slow to close.
    """

    async def _close() -> None:
        try:
            if session is not None:
                await session.aclose()
        except Exception:
            logger.warning("agent session close failed during cleanup")
        finally:
            if provider is not None:
                await provider.aclose()

    task = asyncio.get_running_loop().create_task(_close())
    _track_cleanup_task(task, tasks, "provider close")


def _make_rebuild_agent(
    build_provider: Callable[[ModelConnectionConfig], LLMProvider | None],
    compose: Callable[[LLMProvider, str | None], tuple[AgentSession, Any]],
    provider_box: list[LLMProvider | None],
    session_box: list[AgentSession | None],
    tier_box: list[str | None],
    close_tasks: set[asyncio.Future[Any]],
) -> Callable[[ModelConnectionConfig, str | None], AgentSession | None]:
    """The `:ai` wizard's swap, as one transaction.

    Nothing the app can observe moves until the *whole* replacement —
    provider and the entire session graph over it — exists. A build that
    fails halfway releases only what it built and leaves the live agent
    running, so a mistyped endpoint costs a notification, not the session.
    """

    def rebuild_agent(
        profile: ModelConnectionConfig, model_tier: str | None
    ) -> AgentSession | None:
        new_provider = build_provider(profile)
        if new_provider is None:
            return None
        try:
            new_session, _policy = compose(new_provider, model_tier)
        except Exception:
            _close_provider_in_background(new_provider, close_tasks)
            raise
        old_provider = provider_box[0]
        old_session = session_box[0]
        provider_box[0] = new_provider
        session_box[0] = new_session
        tier_box[0] = model_tier
        _close_agent_in_background(old_session, old_provider, close_tasks)
        return new_session

    return rebuild_agent


def _make_disconnect_agent(
    provider_box: list[LLMProvider | None],
    session_box: list[AgentSession | None],
    close_tasks: set[asyncio.Future[Any]],
) -> Callable[[], None]:
    """`:ai off` (issue #167): release the live session and provider.

    Both boxes are cleared first, so nothing can hand the released pair to
    a caller while the close is in flight. Persisted configuration is
    untouched, so a later wizard rebuild reconnects with the kept
    settings. Idempotent when already off.
    """

    def disconnect_agent() -> None:
        old_provider = provider_box[0]
        old_session = session_box[0]
        provider_box[0] = None
        session_box[0] = None
        if old_provider is not None or old_session is not None:
            _close_agent_in_background(old_session, old_provider, close_tasks)

    return disconnect_agent


def _validate_ca_bundle(path: str | None) -> None:
    """Fail startup actionably when `network.ca_bundle` cannot be loaded.

    Missing, unreadable, and malformed bundles must never silently fall
    back to default trust (issue #168) — a user who configured a corporate
    CA needs to know it is not in effect, not debug TLS errors later.
    """
    if path is None:
        return
    try:
        ssl.create_default_context(cafile=path)
    except (OSError, ssl.SSLError) as exc:
        raise SystemExit(f"korvid: network.ca_bundle {path!r} could not be loaded: {exc}") from exc


@dataclasses.dataclass
class _RunState:
    """What `_run`'s teardown guard must release — filled progressively by
    `_wire_and_run` so a wiring failure releases exactly what was built.

    `close_tasks` retains explicit cleanup work, including replaced agents.
    The task factory retains other descendants until the final sweep.
    `preexisting_tasks` excludes ambient work from that sweep.
    """

    mcp: MCPControllerBase | None = None
    provider_box: list[LLMProvider | None] = dataclasses.field(default_factory=lambda: [None])
    #: The live agent session (issue #166): the teardown guard closes it
    #: before the provider it speaks through, so a partially-wired startup
    #: never tears the transport out from under a session.
    session_box: list[AgentSession | None] = dataclasses.field(default_factory=lambda: [None])
    close_tasks: set[asyncio.Future[Any]] = dataclasses.field(default_factory=set)
    preexisting_tasks: set[asyncio.Task[Any]] | None = None
    discovery_box: list[asyncio.Task[None]] = dataclasses.field(default_factory=list)
    #: Observability connectors (issue #193): each owns an HTTP client
    #: that teardown must close, however far wiring got.
    observability: ObservabilityWiring | None = None


async def _start_mcp_if_enabled(config: KorvidConfig, controller: MCPControllerBase | None) -> None:
    if not config.mcp_enabled or controller is None:
        return
    startup_msg = await controller.start()
    if startup_msg.startswith("ERROR"):
        logger.error("%s", startup_msg)


async def _stop_mcp(state: _RunState) -> None:
    """Bound the controller's stop, retaining any server task it cannot finish.

    Allow its two five-second phases before cancelling the controller itself.
    """
    controller = state.mcp
    if controller is None:
        return
    _track_cleanup_task(controller.pending_task(), state.close_tasks, "MCP server task")

    async def stop() -> None:
        try:
            leftover = await controller.shutdown()
            _track_cleanup_task(leftover, state.close_tasks, "MCP server task")
        finally:
            _track_cleanup_task(controller.pending_task(), state.close_tasks, "MCP server task")

    task = asyncio.create_task(stop())
    _track_cleanup_task(task, state.close_tasks, "MCP shutdown")
    await _drain_cleanup_tasks({task}, timeout=_MCP_SHUTDOWN_GRACE_SECONDS)


def _adopt_run_tasks(state: _RunState) -> None:
    """Retain descendants that survived cancellation of a shielding close wrapper."""
    if state.preexisting_tasks is None:
        return
    current = asyncio.current_task()
    for task in asyncio.all_tasks() - state.preexisting_tasks:
        if task is not current:
            _track_cleanup_task(task, state.close_tasks, "run background task")


async def _finish_run_cleanup(state: _RunState) -> None:
    """Bound the last run-owned tasks before the stdlib runner can gather them.

    Explicit close work has already received grace; cancel remaining descendants
    without another grace wait after the client cleanup attempts.
    Only `_run` enables this sweep, excluding tasks that predate the run and
    the caller itself. Two bounded cancellation sweeps let a finalizer's new
    descendants stop, while repeated respawns remain subject to terminal exit.
    """
    try:
        if state.preexisting_tasks is not None:
            _adopt_run_tasks(state)
            await _drain_cleanup_tasks(state.close_tasks, timeout=0.0)
    finally:
        try:
            if state.preexisting_tasks is not None:
                _adopt_run_tasks(state)
                await _drain_cleanup_tasks(state.close_tasks, timeout=0.0)
        finally:
            _adopt_run_tasks(state)
            _exit_if_cleanup_pending(state.close_tasks)


async def _teardown(state: _RunState, kube: KubeClient) -> None:
    """Attempt every owned cleanup under deadlines, then enforce terminal policy.

    MCP stops accepting work first. Live and replaced agents drain together,
    with sessions preceding their providers. Kubernetes and observability each
    get an independent cleanup budget even when earlier tasks refuse to stop.
    """
    try:
        await _stop_mcp(state)
    finally:
        session = state.session_box[0] if state.session_box else None
        provider = state.provider_box[0] if state.provider_box else None
        state.session_box[:] = [None]
        state.provider_box[:] = [None]
        try:
            await _shutdown(
                state.discovery_box[0] if state.discovery_box else None,
                provider,
                kube,
                session=session,
                close_tasks=state.close_tasks,
            )
        finally:
            try:
                observability = state.observability
                state.observability = None
                if observability is not None:
                    task = asyncio.create_task(observability.aclose())
                    _track_cleanup_task(task, state.close_tasks, "observability client close")
                    await _drain_cleanup_tasks({task})
            finally:
                await _finish_run_cleanup(state)


class _ContextNameResolver(Protocol):
    """Callable shape for the kubeconfig context resolver."""

    def __call__(
        self, context: str | None = None, config_file: str | None = None
    ) -> str | None: ...


def _protected_context_name(
    config: KorvidConfig,
    context: str | None,
    resolve_context_name: _ContextNameResolver,
) -> str | None:
    """Return the effective context when it matches a protection rule."""
    effective = resolve_context_name(context)
    if context_is_protected(effective, config.protected_contexts):
        return effective
    return None
