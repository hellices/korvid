"""Which worker failures the app reports instead of crashing."""

from __future__ import annotations

from korvid.ui.api_resources_controller import API_RESOURCES_GROUP
from korvid.ui.creation_constraints_controller import CREATION_CONSTRAINTS_GROUP
from korvid.ui.worker_failures import REPORTED_WORKER_GROUPS


def test_a_failed_creation_constraints_run_is_reported() -> None:
    assert REPORTED_WORKER_GROUPS[CREATION_CONSTRAINTS_GROUP] == (
        "Workload creation failure explanation failed"
    )


def test_a_failed_api_resources_read_is_reported() -> None:
    assert REPORTED_WORKER_GROUPS[API_RESOURCES_GROUP] == "Reading API resources failed"
