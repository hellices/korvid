"""Re-export of the production audit log for callers that may depend on
`korvid.tools` but not `korvid.core` directly (tach.toml).

`korvid.tools` already depends on `korvid.core`; `korvid.evals` depends on
`korvid.tools` but not `korvid.core`. Importing `AuditLog` from here (not
from `korvid.core.audit` directly) keeps the TUI-free operation-journey
runner (`korvid.evals.operation_runner`) inside its declared module
boundary with no `tach.toml` change.
"""

from __future__ import annotations

from korvid.core.audit import AuditLog

__all__ = ["AuditLog"]
