"""`korvid.tools.audit` re-exports `AuditLog` inside the tach boundary
`korvid.evals` (which does not depend on `korvid.core`) is allowed to
import from.
"""

from __future__ import annotations

from korvid.core.audit import AuditLog as CoreAuditLog
from korvid.tools.audit import AuditLog


def test_tools_audit_reexports_core_audit_log() -> None:
    assert AuditLog is CoreAuditLog
