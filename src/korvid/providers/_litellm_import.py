"""The single `import litellm` in korvid — made offline and silent.

`import litellm` is not side-effect free. In 1.98.0 it calls
`get_model_cost_map(url=...)`, which performs a blocking HTTPS GET of
`model_prices_and_context_window.json` unless `LITELLM_LOCAL_MODEL_COST_MAP`
is already `"true"` in the environment, and warns to **stderr** through a
`StreamHandler` when that fetch fails. Both are unacceptable in a Textual
application: the terminal is korvid's canvas, and a blocking fetch at wiring
time is a startup stall that grows with every firewall between here and
GitHub.

Neither can be fixed after the import, which is why this is a separate
module rather than a few lines at the top of `litellm_runtime`: an import
sorter is free to move a third-party `import litellm` above any `korvid`
import in the same block, so the ordering has to be a *file* boundary. This
module applies no policy of its own — the lockdown lives in
`litellm_runtime`, which is this module's only importer.
"""

from __future__ import annotations

import logging
import os
from types import ModuleType

from korvid.agent.install_hint import isolated_install_hint

# Must be set BEFORE `import litellm`: LiteLLM reads it at module scope and
# never re-reads it. `setdefault`, not assignment, so an operator who
# deliberately exports "false" keeps the remote map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

try:
    import litellm as _litellm
except ImportError as exc:  # pragma: no cover - exercised by the extras tests
    raise ImportError(isolated_install_hint(feature="the embedded agent")) from exc


def _detach_litellm_logging() -> None:
    """Stop LiteLLM writing onto the terminal korvid is drawing on.

    `litellm.verbose_logger` ships with a `StreamHandler` and
    `propagate=True`, so anything it logs lands in the middle of the TUI.
    A `NullHandler` keeps `logging` from installing a last-resort handler
    of its own.
    """
    names = ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router")
    loggers = [getattr(_litellm, "verbose_logger", None)]
    loggers.extend(logging.getLogger(name) for name in names)
    for logger in loggers:
        if logger is None:
            continue
        for handler in list(logger.handlers):
            if isinstance(handler, logging.StreamHandler):
                logger.removeHandler(handler)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False


_detach_litellm_logging()

#: The imported module, re-exported under a name that makes the indirection
#: obvious at the call site.
litellm: ModuleType = _litellm
