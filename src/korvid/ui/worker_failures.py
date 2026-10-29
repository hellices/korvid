"""The worker groups whose failures the app reports instead of crashing.

These workers run with `exit_on_error=False`, so a failure must reach the
user some other way: `KorvidApp.on_worker_state_changed` notifies with the
label kept here. Every other group keeps Textual's default crash-on-error
behaviour, and a cancelled worker never reaches `WorkerState.ERROR`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from korvid.ui.config_propagation_controller import CONFIG_PROPAGATION_GROUP
from korvid.ui.session_timeline_controller import TIMELINE_EVENT_GROUP, TIMELINE_NAVIGATION_GROUP
from korvid.ui.workspace_controller import RELATIONSHIP_GROUP

#: Worker group -> what the notification says failed.
REPORTED_WORKER_GROUPS: Mapping[str, str] = MappingProxyType(
    {
        TIMELINE_EVENT_GROUP: "Warning-event timeline feed failed",
        RELATIONSHIP_GROUP: "Relationships failed",
        TIMELINE_NAVIGATION_GROUP: "Timeline navigation failed",
        CONFIG_PROPAGATION_GROUP: "Configuration propagation failed",
    }
)
