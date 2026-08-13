"""`observability:` config parsing (issue #193).

The section describes *where* a backend is and *how much* it may be
asked; it never carries a credential value, and it has no way to turn TLS
verification off.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from korvid.core.config import ObservabilityBackend, load_config


def _write(tmp_path: Path, body: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


def _load(tmp_path: Path, observability: object) -> tuple[object, object, tuple[str, ...]]:
    config = load_config(_write(tmp_path, {"observability": observability}))
    return config.observability_prometheus, config.observability_loki, config.warnings


class TestAbsence:
    def test_no_section_leaves_both_backends_absent(self, tmp_path: Path) -> None:
        config = load_config(_write(tmp_path, {}))
        assert config.observability_prometheus is None
        assert config.observability_loki is None
        assert config.warnings == ()

    def test_a_non_mapping_section_is_ignored(self, tmp_path: Path) -> None:
        prometheus, loki, _ = _load(tmp_path, "yes please")
        assert prometheus is None
        assert loki is None

    def test_one_backend_can_be_configured_without_the_other(self, tmp_path: Path) -> None:
        prometheus, loki, _ = _load(tmp_path, {"prometheus": {"url": "https://p.example.com"}})
        assert isinstance(prometheus, ObservabilityBackend)
        assert loki is None


class TestUrl:
    def test_the_url_is_kept_verbatim(self, tmp_path: Path) -> None:
        prometheus, _, _ = _load(tmp_path, {"prometheus": {"url": "https://p.example.com/base"}})
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.url == "https://p.example.com/base"

    def test_a_missing_url_disables_the_backend_with_a_warning(self, tmp_path: Path) -> None:
        prometheus, _, warnings = _load(tmp_path, {"prometheus": {"timeout_seconds": 5}})
        assert prometheus is None
        assert any("url" in w for w in warnings)

    @pytest.mark.parametrize("url", ["ftp://p.example.com", "p.example.com", "file:///etc/passwd"])
    def test_a_non_http_url_disables_the_backend(self, tmp_path: Path, url: str) -> None:
        prometheus, _, warnings = _load(tmp_path, {"prometheus": {"url": url}})
        assert prometheus is None
        assert any("http" in w for w in warnings)


class TestTlsCannotBeDisabled:
    @pytest.mark.parametrize(
        "key",
        ["insecure", "insecure_skip_verify", "skip_tls_verify", "tls_skip_verify", "verify"],
    )
    def test_a_verification_switch_disables_the_backend_rather_than_being_ignored(
        self, tmp_path: Path, key: str
    ) -> None:
        """A user who believes they turned verification off must be told they did not."""
        prometheus, _, warnings = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", key: True}}
        )
        assert prometheus is None
        assert any(key in w for w in warnings)

    def test_the_warning_points_at_the_supported_setting(self, tmp_path: Path) -> None:
        _, _, warnings = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", "insecure": True}}
        )
        assert any("network.ca_bundle" in w for w in warnings)

    def test_the_backend_carries_no_verification_field_at_all(self) -> None:
        assert not [f for f in ObservabilityBackend.__dataclass_fields__ if "verif" in f]


class TestCredentialSource:
    def test_an_environment_variable_name_is_kept(self, tmp_path: Path) -> None:
        prometheus, _, _ = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", "token_env": "PROM_TOKEN"}}
        )
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.token_env == "PROM_TOKEN"

    def test_a_token_file_path_is_kept(self, tmp_path: Path) -> None:
        prometheus, _, _ = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", "token_file": "/run/tok"}}
        )
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.token_file == "/run/tok"

    def test_two_credential_sources_disable_the_backend(self, tmp_path: Path) -> None:
        """Guessing which one wins is how the wrong credential gets sent."""
        prometheus, _, warnings = _load(
            tmp_path,
            {
                "prometheus": {
                    "url": "https://p.example.com",
                    "token_env": "PROM_TOKEN",
                    "token_file": "/run/tok",
                }
            },
        )
        assert prometheus is None
        assert any("token_env" in w and "token_file" in w for w in warnings)

    @pytest.mark.parametrize("key", ["token", "password", "bearer_token", "api_key"])
    def test_an_inline_credential_disables_the_backend(self, tmp_path: Path, key: str) -> None:
        """config.yaml is not a secret store; an inline value is a leak, not a setting."""
        prometheus, _, warnings = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", key: "s3cret"}}
        )
        assert prometheus is None
        assert any(key in w for w in warnings)

    def test_the_warning_for_an_inline_credential_does_not_echo_it(self, tmp_path: Path) -> None:
        _, _, warnings = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", "token": "s3cret"}}
        )
        assert not any("s3cret" in w for w in warnings)

    def test_the_backend_has_no_field_that_could_hold_a_credential_value(self) -> None:
        fields = set(ObservabilityBackend.__dataclass_fields__)
        assert {"token", "password", "api_key", "bearer_token"} & fields == set()


class TestLimits:
    def test_limits_default_when_unset(self, tmp_path: Path) -> None:
        prometheus, _, _ = _load(tmp_path, {"prometheus": {"url": "https://p.example.com"}})
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.max_window_minutes > 0
        assert prometheus.max_series > 0
        assert prometheus.max_response_bytes > 0
        assert prometheus.max_concurrency > 0

    def test_a_configured_limit_is_used(self, tmp_path: Path) -> None:
        prometheus, _, _ = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", "max_series": 7}}
        )
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.max_series == 7

    @pytest.mark.parametrize("value", [0, -1, "many", None, True])
    def test_an_unusable_limit_falls_back_to_the_default_with_a_warning(
        self, tmp_path: Path, value: object
    ) -> None:
        prometheus, _, warnings = _load(
            tmp_path, {"prometheus": {"url": "https://p.example.com", "max_series": value}}
        )
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.max_series == ObservabilityBackend.max_series
        assert any("max_series" in w for w in warnings)

    def test_a_default_window_over_the_maximum_is_corrected(self, tmp_path: Path) -> None:
        prometheus, _, warnings = _load(
            tmp_path,
            {
                "prometheus": {
                    "url": "https://p.example.com",
                    "default_window_minutes": 600,
                    "max_window_minutes": 120,
                }
            },
        )
        assert isinstance(prometheus, ObservabilityBackend)
        assert prometheus.default_window_minutes == 120
        assert any("default_window_minutes" in w for w in warnings)


class TestLoki:
    def test_the_tenant_header_value_is_kept(self, tmp_path: Path) -> None:
        _, loki, _ = _load(tmp_path, {"loki": {"url": "https://l.example.com", "tenant": "team-a"}})
        assert isinstance(loki, ObservabilityBackend)
        assert loki.tenant == "team-a"

    def test_label_mappings_default_to_the_common_kubernetes_labels(self, tmp_path: Path) -> None:
        _, loki, _ = _load(tmp_path, {"loki": {"url": "https://l.example.com"}})
        assert isinstance(loki, ObservabilityBackend)
        assert loki.label_mappings["namespace"] == "namespace"
        assert loki.label_mappings["pod"] == "pod"
        assert "workload" in loki.label_mappings

    def test_a_configured_mapping_replaces_only_the_named_scope_field(self, tmp_path: Path) -> None:
        _, loki, _ = _load(
            tmp_path,
            {"loki": {"url": "https://l.example.com", "label_mappings": {"workload": "app_name"}}},
        )
        assert isinstance(loki, ObservabilityBackend)
        assert loki.label_mappings["workload"] == "app_name"
        assert loki.label_mappings["namespace"] == "namespace"

    def test_a_mapping_for_an_unknown_scope_field_is_dropped_with_a_warning(
        self, tmp_path: Path
    ) -> None:
        _, loki, warnings = _load(
            tmp_path,
            {"loki": {"url": "https://l.example.com", "label_mappings": {"cluster": "c"}}},
        )
        assert isinstance(loki, ObservabilityBackend)
        assert "cluster" not in loki.label_mappings
        assert any("cluster" in w for w in warnings)


class TestUrlIsParsedNotPrefixMatched:
    """Round-1 review: a prefix check accepts an authority with no host."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://",
            "https://user:hunter2@",
            "https:///path",
            "http://:8080",
        ],
    )
    def test_a_url_without_a_hostname_disables_the_backend(self, tmp_path: Path, url: str) -> None:
        prometheus, _, warnings = _load(tmp_path, {"prometheus": {"url": url}})
        assert prometheus is None
        assert any("host" in w for w in warnings)

    def test_the_warning_for_a_credential_bearing_url_does_not_echo_it(
        self, tmp_path: Path
    ) -> None:
        _, _, warnings = _load(tmp_path, {"prometheus": {"url": "https://user:hunter2@"}})
        assert not any("hunter2" in w for w in warnings)


class TestTimeoutIsFinite:
    @pytest.mark.parametrize("value", [".inf", ".nan"])
    def test_a_non_finite_timeout_falls_back_to_the_default(
        self, tmp_path: Path, value: str
    ) -> None:
        """YAML `.inf` parses to a float and would mean "no timeout at all"."""
        path = tmp_path / "config.yaml"
        path.write_text(
            "observability:\n  prometheus:\n"
            "    url: https://p.example.com\n"
            f"    timeout_seconds: {value}\n"
        )
        config = load_config(path)
        assert config.observability_prometheus is not None
        assert config.observability_prometheus.timeout_seconds == (
            ObservabilityBackend.timeout_seconds
        )
        assert any("timeout_seconds" in w for w in config.warnings)


class TestLabelMappingsCannotCollide:
    def test_two_scope_fields_mapped_to_one_label_disable_the_backend(self, tmp_path: Path) -> None:
        """The second assignment would overwrite the namespace constraint.

        `{app="prod"}` then `{app="api"}` leaves one matcher, and the
        search silently covers every namespace.
        """
        _, loki, warnings = _load(
            tmp_path,
            {
                "loki": {
                    "url": "https://l.example.com",
                    "label_mappings": {"namespace": "app", "workload": "app"},
                }
            },
        )
        assert loki is None
        assert any("app" in w for w in warnings)

    def test_a_mapping_colliding_with_an_unmapped_default_is_also_refused(
        self, tmp_path: Path
    ) -> None:
        """`workload -> namespace` collides with the default namespace label."""
        _, loki, warnings = _load(
            tmp_path,
            {
                "loki": {
                    "url": "https://l.example.com",
                    "label_mappings": {"workload": "namespace"},
                }
            },
        )
        assert loki is None
        assert any("namespace" in w for w in warnings)

    def test_distinct_mappings_are_accepted(self, tmp_path: Path) -> None:
        _, loki, _ = _load(
            tmp_path,
            {
                "loki": {
                    "url": "https://l.example.com",
                    "label_mappings": {"workload": "service", "pod": "instance"},
                }
            },
        )
        assert isinstance(loki, ObservabilityBackend)


class TestMaskLabels:
    def test_configured_labels_are_kept_normalized(self, tmp_path: Path) -> None:
        _, loki, _ = _load(
            tmp_path,
            {"loki": {"url": "https://l.example.com", "mask_labels": ["Tenant", "customer"]}},
        )
        assert isinstance(loki, ObservabilityBackend)
        assert loki.mask_labels == ("customer", "tenant")

    def test_nothing_configured_masks_nothing(self, tmp_path: Path) -> None:
        _, loki, _ = _load(tmp_path, {"loki": {"url": "https://l.example.com"}})
        assert isinstance(loki, ObservabilityBackend)
        assert loki.mask_labels == ()

    def test_a_non_list_value_is_reported_and_ignored(self, tmp_path: Path) -> None:
        _, loki, warnings = _load(
            tmp_path, {"loki": {"url": "https://l.example.com", "mask_labels": "tenant"}}
        )
        assert isinstance(loki, ObservabilityBackend)
        assert loki.mask_labels == ()
        assert any("mask_labels" in w for w in warnings)

    def test_a_non_string_entry_is_dropped_with_a_warning(self, tmp_path: Path) -> None:
        _, loki, warnings = _load(
            tmp_path, {"loki": {"url": "https://l.example.com", "mask_labels": ["tenant", 7]}}
        )
        assert isinstance(loki, ObservabilityBackend)
        assert loki.mask_labels == ("tenant",)
        assert any("mask_labels" in w for w in warnings)


class TestUrlUserinfoIsAnInlineCredential:
    """Round-4 review: `https://user:pw@host` is a credential in config.yaml.

    The HTTP client would send it as Basic auth, which is exactly the
    inline-credential shape `token:`/`password:` are rejected for.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:hunter2@p.example.com",
            "https://user@p.example.com",
            "https://:hunter2@p.example.com",
        ],
    )
    def test_a_url_carrying_a_credential_disables_the_backend(
        self, tmp_path: Path, url: str
    ) -> None:
        prometheus, _, warnings = _load(tmp_path, {"prometheus": {"url": url}})
        assert prometheus is None
        assert any("token_env" in w for w in warnings)

    def test_the_warning_does_not_echo_the_credential(self, tmp_path: Path) -> None:
        _, _, warnings = _load(
            tmp_path, {"prometheus": {"url": "https://user:hunter2@p.example.com"}}
        )
        assert not any("hunter2" in w for w in warnings)

    def test_a_plain_url_is_still_accepted(self, tmp_path: Path) -> None:
        prometheus, _, _ = _load(tmp_path, {"prometheus": {"url": "https://p.example.com/base"}})
        assert isinstance(prometheus, ObservabilityBackend)


class TestLabelNamesAreValidatedInConfig:
    @pytest.mark.parametrize("name", ['app"} or {x', "app name", "app-name", "1app"])
    def test_a_mapping_to_an_invalid_label_name_disables_the_backend(
        self, tmp_path: Path, name: str
    ) -> None:
        _, loki, warnings = _load(
            tmp_path,
            {"loki": {"url": "https://l.example.com", "label_mappings": {"workload": name}}},
        )
        assert loki is None
        assert any("label name" in w for w in warnings)

    def test_a_conventional_label_name_is_accepted(self, tmp_path: Path) -> None:
        _, loki, _ = _load(
            tmp_path,
            {
                "loki": {
                    "url": "https://l.example.com",
                    "label_mappings": {"workload": "k8s_app_name"},
                }
            },
        )
        assert isinstance(loki, ObservabilityBackend)
