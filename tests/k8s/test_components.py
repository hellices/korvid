"""Component extraction for the hierarchy tree (issue #120).

Helm's rendered ``manifest`` payload field and OLM's object references
(Operator ``status.components.refs``, InstallPlan ``status.plan``) both
reduce to (kind, name, namespace) component rows. The parsers are pure,
cap cluster-controlled input, and skip malformed entries without raising.
"""

from __future__ import annotations

from korvid.k8s.components import (
    MAX_COMPONENT_DOCS,
    ComponentRef,
    installplan_components,
    manifest_components,
    reference_components,
)

_MANIFEST = """\
---
# Source: nginx/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: web-nginx
  namespace: web
---
# Source: nginx/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-nginx
  labels:
    app.kubernetes.io/name: nginx
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: web-nginx-reader
"""


class TestManifestComponents:
    def test_parses_multi_doc_manifest_into_refs(self) -> None:
        refs = manifest_components(_MANIFEST)
        assert refs == [
            ComponentRef(kind="Service", name="web-nginx", api_version="v1", namespace="web"),
            ComponentRef(kind="Deployment", name="web-nginx", api_version="apps/v1"),
            ComponentRef(
                kind="ClusterRole",
                name="web-nginx-reader",
                api_version="rbac.authorization.k8s.io/v1",
            ),
        ]

    def test_missing_namespace_is_empty_string(self) -> None:
        refs = manifest_components("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n")
        assert refs == [ComponentRef(kind="ConfigMap", name="cm", api_version="v1")]

    def test_malformed_doc_is_skipped_but_others_kept(self) -> None:
        manifest = (
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: ok\n"
            "---\n"
            "kind: {unclosed\n"
            "---\n"
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: also-ok\n"
        )
        refs = manifest_components(manifest)
        assert [r.name for r in refs] == ["ok", "also-ok"]

    def test_docs_without_kind_or_name_are_skipped(self) -> None:
        manifest = (
            "---\n"  # empty doc
            "# comment only\n"
            "---\n"
            "kind: Service\n"  # no metadata.name
            "---\n"
            "metadata:\n  name: nameless\n"  # no kind
            "---\n"
            "- just\n- a\n- list\n"  # not a mapping
            "---\n"
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: keep\n"
        )
        refs = manifest_components(manifest)
        assert refs == [ComponentRef(kind="Secret", name="keep", api_version="v1")]

    def test_duplicate_docs_are_deduplicated(self) -> None:
        doc = "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n"
        refs = manifest_components(doc + "---\n" + doc)
        assert len(refs) == 1

    def test_doc_count_is_capped(self) -> None:
        docs = "---\n".join(
            f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm-{i}\n"
            for i in range(MAX_COMPONENT_DOCS + 10)
        )
        refs = manifest_components(docs)
        assert len(refs) == MAX_COMPONENT_DOCS

    def test_oversize_manifest_yields_nothing(self) -> None:
        doc = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n"
        padding = "# " + "x" * (9 * 1024 * 1024) + "\n"
        assert manifest_components(doc + padding) == []

    def test_non_string_manifest_yields_nothing(self) -> None:
        assert manifest_components(None) == []
        assert manifest_components(42) == []

    def test_non_string_scalar_fields_are_stringified_safely(self) -> None:
        manifest = "apiVersion: 1\nkind: Service\nmetadata:\n  name: 123\n  namespace: true\n"
        refs = manifest_components(manifest)
        assert refs == [ComponentRef(kind="Service", name="123", api_version="1", namespace="True")]


class TestReferenceComponents:
    def test_parses_operator_status_refs(self) -> None:
        refs = reference_components(
            [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "cert-manager",
                    "namespace": "operators",
                },
                {"kind": "ClusterRole", "name": "cert-manager-role"},
            ]
        )
        assert refs == [
            ComponentRef(
                kind="Deployment",
                name="cert-manager",
                api_version="apps/v1",
                namespace="operators",
            ),
            ComponentRef(kind="ClusterRole", name="cert-manager-role"),
        ]

    def test_malformed_entries_are_skipped(self) -> None:
        refs = reference_components(["not-a-dict", {"kind": "Role"}, {"name": "kindless"}, None])
        assert refs == []

    def test_non_list_input_yields_nothing(self) -> None:
        assert reference_components(None) == []
        assert reference_components({"kind": "Role", "name": "x"}) == []

    def test_reference_count_is_capped(self) -> None:
        entries = [{"kind": "ConfigMap", "name": f"cm-{i}"} for i in range(MAX_COMPONENT_DOCS + 5)]
        assert len(reference_components(entries)) == MAX_COMPONENT_DOCS

    def test_duplicate_refs_are_deduplicated(self) -> None:
        entry = {"kind": "ConfigMap", "name": "cm", "namespace": "ns"}
        assert len(reference_components([entry, dict(entry)])) == 1


class TestInstallPlanComponents:
    def test_parses_status_plan_steps(self) -> None:
        plan = [
            {
                "resolving": "cert-manager.v1.14.4",
                "resource": {
                    "group": "apps",
                    "version": "v1",
                    "kind": "Deployment",
                    "name": "cert-manager",
                },
                "status": "Created",
            },
            {
                "resource": {"group": "", "version": "v1", "kind": "ServiceAccount", "name": "sa"},
            },
        ]
        refs = installplan_components(plan)
        assert refs == [
            ComponentRef(kind="Deployment", name="cert-manager", api_version="apps/v1"),
            ComponentRef(kind="ServiceAccount", name="sa", api_version="v1"),
        ]

    def test_malformed_steps_are_skipped(self) -> None:
        plan = [
            "not-a-dict",
            {"resource": "not-a-dict"},
            {"resource": {"kind": "Role"}},  # no name
            {"resource": {"name": "kindless"}},  # no kind
            {"resource": {"kind": "Role", "name": "keep"}},
        ]
        assert installplan_components(plan) == [ComponentRef(kind="Role", name="keep")]

    def test_non_list_input_yields_nothing(self) -> None:
        assert installplan_components(None) == []
        assert installplan_components("plan") == []


class TestParserWorkBounds:
    """The caps bound *inspected* input, not accepted refs - a hostile
    payload of malformed or duplicate entries must not buy unbounded work."""

    def test_manifest_docs_beyond_cap_are_never_parsed(self) -> None:
        garbage = "not-an-object\n---\n" * MAX_COMPONENT_DOCS
        tail = "kind: Service\nmetadata:\n  name: late\n"
        assert manifest_components(garbage + tail) == []

    def test_reference_entries_beyond_cap_are_never_inspected(self) -> None:
        entries = [{"kind": "Broken"}] * MAX_COMPONENT_DOCS
        entries.append({"kind": "Service", "name": "late"})
        assert reference_components(entries) == []

    def test_installplan_steps_beyond_cap_are_never_inspected(self) -> None:
        steps: list[object] = ["junk"] * MAX_COMPONENT_DOCS
        steps.append({"resource": {"version": "v1", "kind": "Service", "name": "late"}})
        assert installplan_components(steps) == []

    def test_deeply_nested_document_is_skipped_not_raised(self) -> None:
        bomb = "[" * 200_000
        rest = "---\nkind: Service\nmetadata:\n  name: web\n"
        refs = manifest_components(bomb + "\n" + rest)
        assert refs == [ComponentRef(kind="Service", name="web")]
