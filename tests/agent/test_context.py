"""Cluster context note for the agent system prompt (issue #30).

The note tells the model which cloud provider the cluster runs on so
provider-specific requests ("expose this service publicly") produce
appropriate annotations without the user naming the CSP. No annotation
catalog is shipped — the LLM supplies that knowledge.

`korvid.agent.context.cluster_context_note` is now a thin compatibility
delegate over `korvid.agent.prompt_harness.cluster_context_note` (issue
#316 task 6): these tests pin the `ProviderInfo`-typed entry point
`__main__.py` still calls; the equivalent `ClusterFacts`-typed assertions
live in `tests/agent/test_prompt_harness.py`.
"""

from korvid.agent.context import cluster_context_note
from korvid.k8s.csp import ProviderInfo


def test_unknown_provider_yields_no_note() -> None:
    assert cluster_context_note(ProviderInfo("unknown", None)) is None


def test_aks_note_names_the_managed_distribution() -> None:
    note = cluster_context_note(ProviderInfo("azure", "aks"))
    assert note is not None
    assert "AKS" in note
    assert "Azure" in note


def test_bare_provider_note_names_the_provider() -> None:
    note = cluster_context_note(ProviderInfo("aws", None))
    assert note is not None
    assert "AWS" in note


def test_note_mentions_provider_specific_guidance() -> None:
    """The note must direct the model to answer with provider-appropriate
    annotations even when the user never names the CSP."""
    note = cluster_context_note(ProviderInfo("gcp", "gke"))
    assert note is not None
    assert "annotation" in note.lower()


def test_note_has_no_hardcoded_annotation_keys() -> None:
    """Explicitly rejected: shipping a curated annotation catalog."""
    for info in (
        ProviderInfo("azure", "aks"),
        ProviderInfo("aws", "eks"),
        ProviderInfo("gcp", "gke"),
    ):
        note = cluster_context_note(info)
        assert note is not None
        assert "service.beta.kubernetes.io" not in note
