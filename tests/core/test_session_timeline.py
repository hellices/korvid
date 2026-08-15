from korvid.core.session_timeline import (
    ContextSwitchPayload,
    SessionTimeline,
    TimelineResourceRef,
    TimelineSource,
    WarningEventPayload,
    WatchDeltaPayload,
    WriteAuditPayload,
)


def _warning(message: str, *, uid: str = "pod-uid") -> dict[str, object]:
    return {
        "type": "Warning",
        "reason": "BackOff",
        "message": message,
        "count": 4,
        "lastTimestamp": "2026-08-15T00:00:00Z",
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Pod",
            "namespace": "default",
            "name": "api-1",
            "uid": uid,
        },
    }


def test_append_evicts_oldest_entries_to_hold_count() -> None:
    timeline = SessionTimeline(max_entries=2, max_bytes=4096)

    assert (
        timeline.append_watch(
            epoch=0,
            kind_alias="pods",
            display_kind="Pod",
            namespace="default",
            name="a",
            uid="uid-a",
            verb="ADDED",
        ).accepted
        is True
    )
    assert (
        timeline.append_watch(
            epoch=0,
            kind_alias="pods",
            display_kind="Pod",
            namespace="default",
            name="b",
            uid="uid-b",
            verb="MODIFIED",
        ).accepted
        is True
    )

    result = timeline.append_write(
        epoch=0,
        action="delete",
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="c",
        uid=None,
        outcome="success",
    )

    snap = timeline.snapshot(epoch=None, source=None, resource=None)

    assert result.accepted is True
    assert result.evicted == 1
    assert [entry.resource.name for entry in snap.entries if entry.resource is not None] == [
        "b",
        "c",
    ]
    assert snap.stats.entry_count == 2
    assert snap.stats.evicted == 1


def test_append_evicts_oldest_entries_to_hold_encoded_bytes() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=512)

    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="a" * 100,
        uid="uid-a",
        verb="ADDED",
    )
    result = timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="b" * 100,
        uid="uid-b",
        verb="MODIFIED",
    )

    snap = timeline.snapshot(epoch=None, source=None, resource=None)

    assert result.accepted is True
    assert result.evicted > 0
    assert snap.stats.encoded_bytes <= 512


def test_oversized_entry_is_refused_without_mutating_existing_history() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=320)

    kept = timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="steady",
        uid="uid-steady",
        verb="ADDED",
    )
    refused = timeline.append_warning_event(
        epoch=0,
        event=_warning("Authorization: secret-token " + "x" * 400),
        kind_alias="pods",
    )

    snap = timeline.snapshot(epoch=None, source=None, resource=None)

    assert kept.accepted is True
    assert refused.accepted is False
    assert "too large" in str(refused.diagnostic)
    assert [entry.resource.name for entry in snap.entries if entry.resource is not None] == [
        "steady"
    ]
    assert snap.stats.refused == 1


def test_snapshot_filters_source_epoch_and_recreated_resource_uid_deterministically() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)

    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="api",
        uid="old",
        verb="DELETED",
    )
    timeline.append_watch(
        epoch=1,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="api",
        uid="new",
        verb="ADDED",
    )
    timeline.append_context_switch(
        epoch=1,
        phase="completed",
        from_context="ctx-a",
        to_context="ctx-b",
        note="switched",
    )

    resource = TimelineResourceRef(
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="api",
        uid="new",
    )
    snap = timeline.snapshot(epoch=1, source=TimelineSource.WATCH, resource=resource)

    assert [
        (
            entry.epoch,
            entry.resource.uid if entry.resource is not None else None,
            entry.payload.verb if isinstance(entry.payload, WatchDeltaPayload) else None,
        )
        for entry in snap.entries
    ] == [(1, "new", "ADDED")]


def test_warning_projection_stores_only_normalized_text() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)

    result = timeline.append_warning_event(
        epoch=0,
        event=_warning("Waiting for image pull\n  still pending\t" + "x" * 400),
        kind_alias="pods",
    )

    entry = timeline.snapshot(epoch=None, source=TimelineSource.EVENT, resource=None).entries[0]
    assert isinstance(entry.payload, WarningEventPayload)

    assert result.accepted is True
    assert entry.payload.reason == "BackOff"
    assert "\n" not in entry.payload.note
    assert "\t" not in entry.payload.note
    assert "  " not in entry.payload.note


def test_warning_projection_strips_control_characters_from_structural_fields() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)
    event = _warning("container failed", uid="uid\x1b-1")
    event["lastTimestamp"] = "2026-08-15T00:00:00Z\n\t\x1b[31m"
    event["involvedObject"] = {
        "kind": "Po\n\x1bd",
        "namespace": "def\t\x1bault",
        "name": "api\r\x1b-1",
        "uid": "uid\n\x1b-1",
    }

    result = timeline.append_warning_event(epoch=0, event=event, kind_alias="pods")

    entry = timeline.snapshot(epoch=None, source=TimelineSource.EVENT, resource=None).entries[0]
    assert result.accepted is True
    assert "\x1b" not in entry.occurred_at
    assert "\N{REPLACEMENT CHARACTER}" in entry.occurred_at
    assert entry.resource is not None
    structural_values = (
        entry.resource.display_kind,
        entry.resource.namespace,
        entry.resource.name,
        entry.resource.uid or "",
    )
    assert all("\x1b" not in value for value in structural_values)
    assert all(not {"\n", "\r", "\t"} & set(value) for value in structural_values)
    assert all("\N{REPLACEMENT CHARACTER}" in value for value in structural_values)
    assert not {"\n", "\r", "\t"} & set(entry.occurred_at)


def test_warning_projection_bounds_cluster_authored_text_fields() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)
    event = _warning("m" * 400)
    event["reason"] = "r" * 200
    event["lastTimestamp"] = "t" * 100
    event["involvedObject"] = {
        "kind": "k" * 200,
        "namespace": "s" * 100,
        "name": "n" * 400,
        "uid": "u" * 200,
    }

    result = timeline.append_warning_event(epoch=0, event=event, kind_alias="pods")

    entry = timeline.snapshot(epoch=None, source=TimelineSource.EVENT, resource=None).entries[0]
    assert isinstance(entry.payload, WarningEventPayload)
    assert result.accepted is True
    assert len(entry.payload.reason) == 128
    assert len(entry.payload.note) == 240
    assert len(entry.occurred_at) == 64
    assert entry.resource is not None
    assert len(entry.resource.display_kind) == 128
    assert len(entry.resource.namespace) == 63
    assert len(entry.resource.name) == 253
    assert len(entry.resource.uid or "") == 128


def test_warning_projection_redacts_credentials_before_storage() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)

    result = timeline.append_warning_event(
        epoch=0,
        event=_warning("Authorization: secret-token\nBack-off"),
        kind_alias="pods",
    )

    entry = timeline.snapshot(epoch=None, source=TimelineSource.EVENT, resource=None).entries[0]
    assert isinstance(entry.payload, WarningEventPayload)

    assert result.accepted is True
    assert "secret-token" not in entry.payload.note
    assert "••••••" in entry.payload.note
    assert "\n" not in entry.payload.note


def test_context_switch_note_is_bounded_after_whitespace_normalization() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)

    result = timeline.append_context_switch(
        epoch=0,
        phase="failed",
        from_context="ctx-a",
        to_context="ctx-b",
        note=("failure detail\n" * 40),
    )

    entry = timeline.snapshot(epoch=None, source=TimelineSource.CONTEXT, resource=None).entries[0]
    assert isinstance(entry.payload, ContextSwitchPayload)
    assert result.accepted is True
    assert len(entry.payload.note) == 160
    assert "\n" not in entry.payload.note


def test_write_outcome_is_bounded_after_whitespace_normalization() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)

    result = timeline.append_write(
        epoch=0,
        action="delete",
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="api-1",
        uid=None,
        outcome=("error detail\n" * 40),
    )

    entry = timeline.snapshot(epoch=None, source=TimelineSource.WRITE, resource=None).entries[0]
    assert isinstance(entry.payload, WriteAuditPayload)
    assert result.accepted is True
    assert len(entry.payload.outcome) == 160
    assert "\n" not in entry.payload.outcome
