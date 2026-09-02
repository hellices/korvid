"""Direct tests for `ResourceInspectController` (issue #187 / Deep Task 9).

The controller owns everything the user's read-only inspection keys do:
`d` (describe the selected row), the hierarchy tree's named describe, the
Secret masking rule those two share, the provider footer, `Enter` on a pod
(container rows, then shell or logs), and the `h` hint-details overlay -
together with the stale-context and identity guards each of those needs
after an awaited fetch.

Everything arrives as an injected interface, so none of it needs a running
app: `UiSurface` for toasts/modals/workers, `ViewState` for the selection,
`ContextGuard` for the `:ctx` epoch, and `InspectSurface` for the two
mounted widgets (the row cursor and the hint strip).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, get_type_hints

from korvid.core.audit import AuditLog
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.ui.hints import EventsFetcher
from korvid.ui.log_controller import StreamLogsFn
from korvid.ui.resource_inspect_controller import InspectSurface, ResourceInspectController
from korvid.ui.widgets.containers_screen import ContainersScreen
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.widgets.secret_screen import SecretScreen

from .test_write_coordinator import FakeContext, FakeUi, FakeView


async def _fake_stream_logs(*args: Any, **kwargs: Any) -> AsyncIterator[LogLine]:
    """A log source with the shared stream signature; never iterated here -
    the container pick only checks that a source exists."""
    return
    yield  # pragma: no cover - makes this an async generator


_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))
_ALIASES = {"pods": _PODS_META, "services": _SVC_META}

_POD_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "api-1", "namespace": "default", "uid": "uid-1"},
    "spec": {"containers": [{"name": "app", "image": "nginx:1"}]},
    "status": {
        "containerStatuses": [
            {"name": "app", "image": "nginx:1", "ready": True, "restartCount": 0, "state": {}}
        ]
    },
}

_SECRET_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "creds", "namespace": "default"},
    "data": {"password": "aHVudGVyMg=="},
}

_LB_SERVICE_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "api", "namespace": "default"},
    "spec": {"type": "LoadBalancer"},
}


def _pod(
    name: str = "api-1",
    *,
    uid: str = "uid-1",
    trouble: tuple[ContainerTrouble, ...] = (),
    phase: str = "Running",
    containers: tuple[str, ...] = ("app",),
) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase=phase,
        ready="1/1",
        restarts=0,
        node="node-1",
        containers=containers,
        uid=uid,
        trouble=trouble,
    )


_TROUBLE = (ContainerTrouble(container="app", reason="CrashLoopBackOff", message="boom"),)


class FakeSurface(InspectSurface):
    """The two mounted widgets: the row cursor and the hint strip."""

    def __init__(self, row_key: str | None = "default/api-1") -> None:
        self.row_key = row_key
        self.troubles: list[tuple[tuple[ContainerTrouble, ...], str | None]] = []
        self.cleared = 0

    def cursor_row_key(self) -> str | None:
        return self.row_key

    def show_trouble(
        self, trouble: tuple[ContainerTrouble, ...], *, event: str | None = None
    ) -> None:
        self.troubles.append((trouble, event))

    def clear_hint(self) -> None:
        self.cleared += 1


class FakeShell:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def run_shell(self, namespace: str, name: str, container: str) -> None:
        self.calls.append((namespace, name, container))


class FakeLogs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []

    async def open_pane(self, namespace: str, targets: list[tuple[str, str]]) -> None:
        self.calls.append((namespace, targets))


class FakeEvents(EventsFetcher):
    """`EventsFetcher` double with a scripted answer or failure."""

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        error: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self.events = events if events is not None else []
        self.error = error
        self.hang = hang
        self.calls: list[tuple[str, str, str | None]] = []

    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((namespace, name, uid))
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return self.events


class Harness:
    """A controller over fake Textual/view/context/widget surfaces."""

    def __init__(
        self,
        *,
        kind: str = "pods",
        selected: tuple[str | None, str | None] = ("default", "api-1"),
        rows: list[Any] | None = None,
        manifest: dict[str, Any] | None = None,
        manifest_error: BaseException | None = None,
        events: FakeEvents | None = None,
        provider_hint: str | None = None,
        stream_logs: bool = True,
        row_key: str | None = "default/api-1",
        audit: AuditLog | None = None,
        current_uid: str | None = "uid-1",
        uid_error: BaseException | None = None,
        get_manifest: bool = True,
        cross_on_fetch: bool = False,
    ) -> None:
        self.ui = FakeUi()
        self.view = FakeView(
            kind=kind,
            selected=selected,
            aliases=dict(_ALIASES),
            rows=rows if rows is not None else [_pod()],
        )
        self.context = FakeContext()
        self.surface = FakeSurface(row_key)
        self.shell = FakeShell()
        self.logs = FakeLogs()
        self.events = events
        self.manifests: list[tuple[str, str | None, str]] = []
        self._manifest = manifest if manifest is not None else _POD_MANIFEST
        self._manifest_error = manifest_error
        self.current_uid = current_uid
        self.uid_error = uid_error
        self._cross_on_fetch = cross_on_fetch
        self.controller = ResourceInspectController(
            ui=self.ui,
            view=self.view,
            context=self.context,
            surface=self.surface,
            shell=lambda: self.shell,
            logs=lambda: self.logs,
            get_manifest=lambda: self._get_manifest if get_manifest else None,
            get_events=lambda: self.events,
            stream_logs=lambda: _fake_stream_logs if stream_logs else None,
            target_uid=self._target_uid,
            audit=lambda: audit,
            provider_hint=lambda: provider_hint,
        )

    async def _get_manifest(self, kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        self.manifests.append((kind, namespace, name))
        if self._cross_on_fetch:
            self.context.value += 1  # a `:ctx` switch completes mid-fetch
        if self._manifest_error is not None:
            raise self._manifest_error
        return self._manifest

    async def _target_uid(self, kind: str, namespace: str | None, name: str) -> str | None:
        if self.uid_error is not None:
            raise self.uid_error
        return self.current_uid

    def screen(self) -> Any:
        return self.ui.screens[-1][0]

    def answer(self, result: Any) -> None:
        _screen, callback = self.ui.screens[-1]
        assert callback is not None
        callback(result)


# ---------------------------------------------------------------------------
# describe (selected row)
# ---------------------------------------------------------------------------


async def test_describe_reports_when_no_manifest_source_is_wired() -> None:
    h = Harness(get_manifest=False)
    await h.controller.describe_selected()
    assert h.ui.screens == []
    assert "Describe unavailable" in h.ui.messages()


async def test_describe_refuses_during_a_context_switch() -> None:
    h = Harness()
    h.context.reads = False
    await h.controller.describe_selected()
    assert h.ui.screens == []
    assert h.manifests == []


async def test_describe_pushes_the_manifest_with_its_events() -> None:
    h = Harness(events=FakeEvents([{"reason": "Pulled"}]))
    await h.controller.describe_selected()
    screen = h.screen()
    assert isinstance(screen, DescribeScreen)
    assert h.events is not None
    assert h.events.calls == [("default", "api-1", None)]


async def test_describe_masks_a_secret_in_the_dedicated_viewer() -> None:
    """Spec §5 #9: a Secret never renders in the plain describe screen."""
    h = Harness(manifest=_SECRET_MANIFEST)
    await h.controller.describe_selected()
    screen = h.screen()
    assert isinstance(screen, SecretScreen)
    assert not isinstance(screen, DescribeScreen)


async def test_describe_reports_an_api_error_without_a_screen() -> None:
    h = Harness(manifest_error=ApiStatusError(403, "Forbidden", "nope"))
    await h.controller.describe_selected()
    assert h.ui.screens == []
    assert any(severity == "error" for _message, severity in h.ui.notifications)


async def test_describe_is_cancelled_by_a_context_switch_during_the_fetch() -> None:
    h = Harness(cross_on_fetch=True)
    await h.controller.describe_selected()
    assert h.ui.screens == []
    assert any("the kube context changed" in message for message in h.ui.messages())


async def test_describe_events_are_pods_only() -> None:
    """Events are matched by involvedObject.name only, so a Service must
    not show a same-named pod's events."""
    h = Harness(
        kind="services",
        selected=("default", "api"),
        manifest=_LB_SERVICE_MANIFEST,
        events=FakeEvents([{"reason": "Pulled"}]),
    )
    await h.controller.describe_selected()
    assert h.events is not None
    assert h.events.calls == []


async def test_describe_carries_the_provider_footer() -> None:
    h = Harness(
        kind="services",
        selected=("default", "api"),
        manifest=_LB_SERVICE_MANIFEST,
        provider_hint="aks",
    )
    await h.controller.describe_selected()
    screen = h.screen()
    assert isinstance(screen, DescribeScreen)
    assert h.controller.provider_footer(_LB_SERVICE_MANIFEST) is not None


# ---------------------------------------------------------------------------
# describe (named target, from the hierarchy tree)
# ---------------------------------------------------------------------------


async def test_describe_named_pushes_the_named_object() -> None:
    h = Harness()
    await h.controller.describe_named("deployments", "default", "web")
    assert isinstance(h.screen(), DescribeScreen)
    assert h.manifests == [("deployments", "default", "web")]


async def test_describe_named_masks_secrets_too() -> None:
    h = Harness(manifest=_SECRET_MANIFEST)
    await h.controller.describe_named("secrets", "default", "creds")
    assert isinstance(h.screen(), SecretScreen)


async def test_describe_named_reports_a_missing_manifest_source() -> None:
    h = Harness(get_manifest=False)
    await h.controller.describe_named("deployments", "default", "web")
    assert "Describe unavailable" in h.ui.messages()


async def test_describe_named_refuses_during_a_context_switch() -> None:
    h = Harness()
    h.context.reads = False
    await h.controller.describe_named("deployments", "default", "web")
    assert h.manifests == []


# ---------------------------------------------------------------------------
# containers (Enter on a pod row)
# ---------------------------------------------------------------------------


async def test_open_containers_reports_a_pod_without_containers() -> None:
    h = Harness(rows=[_pod(containers=())], manifest={"spec": {}, "status": {}})
    await h.controller.open_containers("default", "api-1")
    assert h.ui.screens == []
    assert "No containers found for this pod" in h.ui.messages()


async def test_open_containers_offers_shell_and_logs_per_container() -> None:
    h = Harness()
    await h.controller.open_containers("default", "api-1")
    assert isinstance(h.screen(), ContainersScreen)
    h.answer(("shell", "app"))
    assert h.shell.calls == [("default", "api-1", "app")]


async def test_container_logs_pick_opens_the_log_pane() -> None:
    h = Harness()
    await h.controller.open_containers("default", "api-1")
    h.answer(("logs", "app"))
    await h.ui.settle()
    assert h.logs.calls == [("default", [("api-1", "app")])]


async def test_container_logs_report_a_missing_log_stream() -> None:
    h = Harness(stream_logs=False)
    await h.controller.open_containers("default", "api-1")
    h.answer(("logs", "app"))
    await h.ui.settle()
    assert h.logs.calls == []
    assert "Log streaming unavailable" in h.ui.messages()


async def test_container_pick_after_a_context_switch_is_refused() -> None:
    h = Harness()
    await h.controller.open_containers("default", "api-1")
    h.context.value += 1
    h.answer(("shell", "app"))
    assert h.shell.calls == []
    assert any("the kube context" in message for message in h.ui.messages())


async def test_container_rows_fetched_across_a_switch_open_nothing() -> None:
    h = Harness(cross_on_fetch=True)
    await h.controller.open_containers("default", "api-1")
    assert h.ui.screens == []


async def test_container_rows_fall_back_to_the_store_when_the_fetch_fails() -> None:
    h = Harness(manifest_error=ApiStatusError(500, "ServerError", "boom"))
    rows = await h.controller.build_container_rows("default", "api-1")
    assert rows == [("app", "-", "-", "-", "-")]


# ---------------------------------------------------------------------------
# hint details (`h`)
# ---------------------------------------------------------------------------


async def test_hint_details_is_a_pods_only_overlay() -> None:
    h = Harness(kind="services")
    h.controller.hint_details()
    assert h.ui.workers == []


async def test_hint_details_ignores_a_healthy_pod() -> None:
    h = Harness()
    h.controller.hint_details()
    assert h.ui.workers == []


async def test_hint_details_ignores_an_empty_cursor() -> None:
    h = Harness(rows=[_pod(trouble=_TROUBLE)], row_key=None)
    h.controller.hint_details()
    assert h.ui.workers == []


async def test_hint_details_starts_for_a_troubled_pod() -> None:
    h = Harness(rows=[_pod(trouble=_TROUBLE)])
    h.controller.hint_details()
    assert len(h.ui.workers) == 1
    await h.ui.settle()


async def test_open_hint_details_shows_trouble_and_events() -> None:
    h = Harness(rows=[_pod(trouble=_TROUBLE)], events=FakeEvents([{"reason": "BackOff"}]))
    await h.controller.open_hint_details("default/api-1", _pod(trouble=_TROUBLE))
    screen = h.screen()
    assert isinstance(screen, HintDetailScreen)
    assert h.events is not None
    assert h.events.calls == [("default", "api-1", "uid-1")]


async def test_open_hint_details_states_unavailable_events() -> None:
    """A failed events fetch is stated, never conflated with "no events"."""
    h = Harness(
        rows=[_pod(trouble=_TROUBLE)],
        events=FakeEvents(error=ApiStatusError(403, "Forbidden", "nope")),
    )
    await h.controller.open_hint_details("default/api-1", _pod(trouble=_TROUBLE))
    screen = h.screen()
    assert isinstance(screen, HintDetailScreen)
    assert screen._events_unavailable is True


async def test_open_hint_details_yields_to_another_dialog() -> None:
    h = Harness(rows=[_pod(trouble=_TROUBLE)])
    h.ui.depth = 2
    await h.controller.open_hint_details("default/api-1", _pod(trouble=_TROUBLE))
    assert h.ui.screens == []


async def test_open_hint_details_drops_a_moved_cursor() -> None:
    h = Harness(rows=[_pod(trouble=_TROUBLE)], row_key="default/other")
    await h.controller.open_hint_details("default/api-1", _pod(trouble=_TROUBLE))
    assert h.ui.screens == []


async def test_open_hint_details_drops_a_replaced_pod() -> None:
    h = Harness(rows=[_pod(uid="uid-2", trouble=_TROUBLE)])
    await h.controller.open_hint_details("default/api-1", _pod(uid="uid-1", trouble=_TROUBLE))
    assert h.ui.screens == []


async def test_open_hint_details_drops_a_recovered_pod() -> None:
    h = Harness(rows=[_pod()])
    await h.controller.open_hint_details("default/api-1", _pod(trouble=_TROUBLE))
    assert h.ui.screens == []


# ---------------------------------------------------------------------------
# store lookups and the pod identity guard
# ---------------------------------------------------------------------------


async def test_pod_containers_come_from_the_store() -> None:
    h = Harness(rows=[_pod(containers=("app", "sidecar"))])
    assert h.controller.pod_containers("default", "api-1") == ("app", "sidecar")
    assert h.controller.pod_containers("default", "missing") == ()


async def test_find_pod_summary_needs_a_namespaced_row_key() -> None:
    h = Harness()
    assert h.controller.find_pod_summary("default/api-1") is not None
    assert h.controller.find_pod_summary("api-1") is None


async def test_pod_uid_unchanged_accepts_the_same_incarnation() -> None:
    h = Harness(current_uid="uid-1")
    assert await h.controller.pod_uid_unchanged("default", "api-1", "uid-1", action="Transfer")


async def test_pod_uid_unchanged_refuses_a_replacement() -> None:
    h = Harness(current_uid="uid-2")
    assert not await h.controller.pod_uid_unchanged("default", "api-1", "uid-1", action="Transfer")
    assert any("was replaced" in message for message in h.ui.messages())


async def test_pod_uid_unchanged_refuses_a_vanished_pod() -> None:
    h = Harness(uid_error=ApiStatusError(404, "NotFound", "gone"))
    assert not await h.controller.pod_uid_unchanged("default", "api-1", "uid-1", action="Transfer")
    assert any("no longer exists" in message for message in h.ui.messages())


async def test_pod_uid_unchanged_refuses_an_unverifiable_pod() -> None:
    h = Harness(current_uid=None)
    assert not await h.controller.pod_uid_unchanged("default", "api-1", "uid-1", action="Transfer")
    messages = h.ui.messages()
    assert any("could not be verified" in message and "Retry" in message for message in messages)
    assert all(
        "no longer exists" not in message and "was replaced" not in message for message in messages
    )


async def test_pod_uid_unchanged_refuses_a_missing_approved_uid() -> None:
    h = Harness(current_uid="uid-1")
    assert not await h.controller.pod_uid_unchanged("default", "api-1", None, action="Transfer")
    assert any(
        "could not be verified" in message and "Retry" in message for message in h.ui.messages()
    )


async def test_provider_footer_is_none_without_a_detected_provider() -> None:
    h = Harness()
    assert h.controller.provider_footer(_LB_SERVICE_MANIFEST) is None


# ---------------------------------------------------------------------------
# The log-stream port is typed, not `Any`
# ---------------------------------------------------------------------------


def test_the_log_stream_port_carries_the_shared_stream_signature() -> None:
    """`Any` here would let a mistyped log source through silently: the pick
    hands whatever this returns to `LogController`, which calls it with the
    stream signature. The port names that signature."""
    hints = get_type_hints(ResourceInspectController.__init__)
    assert hints["stream_logs"] == Callable[[], StreamLogsFn | None]
