from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("litellm")

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


def test_litellm_logging_cannot_reach_the_terminal() -> None:
    """LiteLLM ships `verbose_logger` with a `StreamHandler` and
    `propagate=True`; in a Textual app that is a corrupted screen."""
    import logging

    import litellm

    import korvid.providers.litellm_runtime  # noqa: F401 - import applies the fix

    logger = litellm.verbose_logger
    assert not any(type(h) is logging.StreamHandler for h in logger.handlers), logger.handlers
    assert logger.propagate is False
