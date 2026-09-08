from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest

#: The two tests below drive the real SDK. The forced-environment test does
#: not — it stands a stub in for `litellm` precisely so the ordering can be
#: proven in a base installation too.
_needs_litellm = pytest.mark.skipif(
    importlib.util.find_spec("litellm") is None,
    reason="the [agent] extra is not installed",
)

_PROBE = textwrap.dedent(
    """
    import socket
    import sys

    attempts = []

    def _refuse(self, address):  # noqa: ANN001, ANN202
        attempts.append(address)
        raise OSError("network disabled for this probe")

    socket.socket.connect = _refuse
    socket.socket.connect_ex = lambda self, address: (attempts.append(address), 1)[1]

    from korvid.providers.litellm_runtime import models_by_provider

    table = models_by_provider()
    assert table, "empty provider table"
    total = sum(len(models) for models in table.values())
    assert total > 0

    print(len(attempts))
    """
)


#: Stands a recording stub in for the SDK so the *ordering* can be proven
#: without the [agent] extra and without a byte of network: the stub's
#: `exec_module` runs at exactly the moment `import litellm` runs, which is
#: the moment the real SDK reads the variable at its own module scope.
_FORCED_ENV_PROBE = textwrap.dedent(
    """
    import importlib.machinery
    import logging
    import os
    import socket
    import sys

    attempts = []

    def _refuse(self, address):  # noqa: ANN001, ANN202
        attempts.append(address)
        raise OSError("network disabled for this probe")

    socket.socket.connect = _refuse
    socket.socket.connect_ex = lambda self, address: (attempts.append(address), 1)[1]

    seen = {}

    class _RecordingLiteLLMStub:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN202
            if fullname != "litellm":
                return None
            return importlib.machinery.ModuleSpec(fullname, self)

        def create_module(self, spec):  # noqa: ANN001, ANN202
            return None

        def exec_module(self, module):  # noqa: ANN001, ANN202
            seen["at_import"] = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")
            module.verbose_logger = logging.getLogger("stub-litellm")

    sys.meta_path.insert(0, _RecordingLiteLLMStub())

    import korvid.providers._litellm_import as shim

    assert shim.litellm.__name__ == "litellm", shim.litellm
    assert attempts == [], attempts
    print(seen.get("at_import"), os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"))
    """
)


def test_an_ambient_false_cannot_re_enable_the_startup_fetch() -> None:
    """A hostile ambient value must not decide korvid's import.

    `LITELLM_LOCAL_MODEL_COST_MAP=false` in the environment korvid is
    started from — a stale profile, a CI image, an operator experiment —
    used to survive into `import litellm` and re-arm the blocking HTTPS
    fetch of the price table at wiring time. The variable is forced for
    this process instead, so what the SDK reads is korvid's answer.
    """
    result = subprocess.run(
        [sys.executable, "-c", _FORCED_ENV_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "false"},
    )

    assert result.returncode == 0, result.stderr
    # First value: what the import itself saw. Second: what the process
    # carries afterwards, which is what a re-import would see.
    assert result.stdout.split() == ["true", "true"], result.stdout


@_needs_litellm
def test_importing_the_provider_layer_opens_no_socket() -> None:
    """`import litellm` fetches the remote cost map over HTTPS unless
    `LITELLM_LOCAL_MODEL_COST_MAP` is already set. Measured on 1.98.0:
    4 connections to 185.199.x.x:443 without it, 0 with it. The wrapper
    sets it before the import, which is the only place it can be set.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", result.stdout


@_needs_litellm
def test_litellm_logging_cannot_reach_the_terminal() -> None:
    """LiteLLM ships `verbose_logger` with a `StreamHandler` and
    `propagate=True`; in a Textual app that is a corrupted screen."""
    import logging

    import litellm

    import korvid.providers.litellm_runtime  # noqa: F401 - import applies the fix

    logger = getattr(litellm, "verbose_logger", None)
    # The same attribute guard `_litellm_import._detach_litellm_logging` uses:
    # the SDK re-exports `verbose_logger` without declaring it public, so the
    # runtime contract — not the module's export list — is what korvid checks.
    assert isinstance(logger, logging.Logger), "litellm no longer ships `verbose_logger`"
    assert not any(type(h) is logging.StreamHandler for h in logger.handlers), logger.handlers
    assert logger.propagate is False
