"""Composition-root wiring for the observability connectors (issue #193).

The connectors are optional in two independent ways: the extra may be
absent, and the config section may be absent. These tests pin what each
combination does, and that the TLS trust the rest of korvid uses is the
trust these clients get.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from korvid.__main__ import _build_observability
from korvid.core.config import KorvidConfig, ObservabilityBackend


def _config(**kwargs: Any) -> KorvidConfig:
    return dataclasses.replace(KorvidConfig(), **kwargs)


class TestAbsence:
    async def test_no_configuration_yields_no_connectors(self) -> None:
        wiring = _build_observability(_config())
        assert wiring.metrics is None
        assert wiring.logs is None
        assert wiring.backends == frozenset()
        await wiring.aclose()

    async def test_closing_an_empty_wiring_is_safe(self) -> None:
        await _build_observability(_config()).aclose()


class TestPresence:
    async def test_a_prometheus_url_builds_only_the_metrics_connector(self) -> None:
        wiring = _build_observability(
            _config(observability_prometheus=ObservabilityBackend(url="https://p.example.com"))
        )
        try:
            assert wiring.metrics is not None
            assert wiring.logs is None
            assert wiring.backends == frozenset({"metrics"})
        finally:
            await wiring.aclose()

    async def test_a_loki_url_builds_only_the_logs_connector(self) -> None:
        wiring = _build_observability(
            _config(observability_loki=ObservabilityBackend(url="https://l.example.com"))
        )
        try:
            assert wiring.logs is not None
            assert wiring.metrics is None
            assert wiring.backends == frozenset({"logs"})
        finally:
            await wiring.aclose()

    async def test_both_backends_can_be_configured_together(self) -> None:
        wiring = _build_observability(
            _config(
                observability_prometheus=ObservabilityBackend(url="https://p.example.com"),
                observability_loki=ObservabilityBackend(url="https://l.example.com"),
            )
        )
        try:
            assert wiring.backends == frozenset({"metrics", "logs"})
        finally:
            await wiring.aclose()

    async def test_the_configured_limits_reach_the_connector(self) -> None:
        wiring = _build_observability(
            _config(
                observability_prometheus=ObservabilityBackend(
                    url="https://p.example.com", max_concurrency=5
                )
            )
        )
        try:
            assert wiring.metrics is not None
            assert wiring.metrics.max_concurrency == 5
        finally:
            await wiring.aclose()

    async def test_closing_the_wiring_closes_every_client(self) -> None:
        wiring = _build_observability(
            _config(
                observability_prometheus=ObservabilityBackend(url="https://p.example.com"),
                observability_loki=ObservabilityBackend(url="https://l.example.com"),
            )
        )
        metrics, logs = wiring.metrics, wiring.logs
        await wiring.aclose()
        assert metrics is not None
        assert logs is not None
        assert metrics._http._client.is_closed
        assert logs._http._client.is_closed


class TestTrust:
    async def test_an_unloadable_ca_bundle_fails_startup_rather_than_degrading(
        self, tmp_path: Path
    ) -> None:
        """Falling back to default trust would silently ignore the setting."""
        missing = tmp_path / "absent.pem"
        with pytest.raises(SystemExit, match="ca_bundle"):
            _build_observability(
                _config(
                    network_ca_bundle=str(missing),
                    observability_prometheus=ObservabilityBackend(url="https://p.example.com"),
                )
            )

    async def test_the_client_comes_from_the_shared_trust_builder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One trust configuration for every korvid-owned HTTPS client.

        A connector that built its own client could disagree with the
        providers about `network.ca_bundle` — and would be the one place
        able to express "do not verify".
        """
        import httpx

        import korvid.providers.net as net

        seen: list[str | None] = []
        original = net.make_client

        def spy(ca_bundle: str | None, timeout: Any) -> httpx.AsyncClient:
            seen.append(ca_bundle)
            return original(ca_bundle, timeout)

        monkeypatch.setattr(net, "make_client", spy)
        wiring = _build_observability(
            _config(
                network_ca_bundle=None,
                observability_prometheus=ObservabilityBackend(url="https://p.example.com"),
            )
        )
        await wiring.aclose()
        assert seen == [None]

    def test_the_connector_package_never_constructs_its_own_client(self) -> None:
        """Structural: `korvid.obs` has no way to express `verify=False`.

        Read from the syntax tree rather than the text, so prose about
        TLS in a docstring neither passes nor fails the check.
        """
        import ast

        obs = Path(__file__).resolve().parents[1] / "src" / "korvid" / "obs"
        offenders: list[str] = []
        for path in sorted(obs.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                if any(kw.arg == "verify" for kw in node.keywords):
                    offenders.append(f"{path.name}: passes verify=")
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr in (
                    "AsyncClient",
                    "Client",
                    "_create_unverified_context",
                ):
                    offenders.append(f"{path.name}: constructs {target.attr}")
        assert offenders == []


class TestMissingExtra:
    async def test_a_configured_backend_without_the_extra_fails_actionably(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import korvid.__main__ as main

        monkeypatch.setattr(main, "_missing_extra_packages", lambda roots: ["httpx"])
        with pytest.raises(SystemExit, match=r"korvid\[observability\]"):
            _build_observability(
                _config(observability_prometheus=ObservabilityBackend(url="https://p.example.com"))
            )

    async def test_no_configured_backend_without_the_extra_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import korvid.__main__ as main

        monkeypatch.setattr(main, "_missing_extra_packages", lambda roots: ["httpx"])
        wiring = _build_observability(_config())
        assert wiring.backends == frozenset()


class TestConnectorRefusalAtStartup:
    async def test_a_connector_config_refusal_fails_startup_actionably(self) -> None:
        """A refusal the config parser did not catch must not be a traceback."""
        backend = ObservabilityBackend(
            url="https://l.example.com",
            label_mappings={"namespace": "app", "pod": "pod", "workload": "app"},
        )
        with pytest.raises(SystemExit, match="app"):
            _build_observability(_config(observability_loki=backend))
