"""Post-acceptance observer isolation for durably audited writes."""

from __future__ import annotations

import asyncio
import logging

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.writes import WriteMutationResult
from korvid.ui.ui_surface import UiSurface
from korvid.ui.write_gate import AcceptedWriteObserver, AcceptedWriteReceipt

logger = logging.getLogger(__name__)


async def notify_accepted_observer(
    ui: UiSurface,
    action: str,
    meta: ResourceMeta,
    namespace: str | None,
    name: str,
    observer: AcceptedWriteObserver,
    mutation: WriteMutationResult | None,
) -> None:
    """Run optional verification without rewriting an accepted write result."""
    receipt = AcceptedWriteReceipt.now(
        action=action,
        meta=meta,
        namespace=namespace,
        name=name,
        mutation=mutation,
    )
    try:
        observing = asyncio.ensure_future(observer(receipt))
    except (Exception, asyncio.CancelledError) as exc:
        _warn(ui, action, meta, name, exc)
        return
    try:
        await asyncio.shield(observing)
    except asyncio.CancelledError as exc:
        if not observing.cancelled():
            observing.cancel()
            await asyncio.gather(observing, return_exceptions=True)
            raise
        _warn(ui, action, meta, name, exc)
    except Exception as exc:
        _warn(ui, action, meta, name, exc)


def _warn(
    ui: UiSurface,
    action: str,
    meta: ResourceMeta,
    name: str,
    exc: BaseException,
) -> None:
    logger.error("accepted-write observer failed: %s", exc, exc_info=exc)
    ui.notify(
        f"{action} {meta.plural}/{name} accepted, but outcome verification "
        f"could not start: {str(exc) or type(exc).__name__}",
        severity="warning",
    )
