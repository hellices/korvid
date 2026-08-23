"""Compatibility delegate for the cluster context note (issue #30).

The formatting logic moved to `korvid.agent.prompt_harness.cluster_context_note`
as a pure function of Task 1's `ClusterFacts` (issue #316 task 6 / design
doc §7), so the prompt harness can compose it without depending on the
k8s-layer `ProviderInfo` type. This module survives only because
`__main__.py` still probes a `ProviderInfo` at connection time and calls
this exact name; it is deleted in issue #316 task 14 once the composition
root converts that probe into `ClusterFacts` before crossing the
boundary, per the design doc's "no preformatted prompt string crosses the
boundary" rule.
"""

from __future__ import annotations

from korvid.agent.interaction import ClusterFacts
from korvid.agent.prompt_harness import cluster_context_note as _cluster_context_note
from korvid.k8s.csp import ProviderInfo


def cluster_context_note(info: ProviderInfo) -> str | None:
    """Build the system prompt note for a detected cloud provider.

    Args:
        info: Detection result from `korvid.k8s.csp.detect_provider`.

    Returns:
        See `korvid.agent.prompt_harness.cluster_context_note`.
    """
    return _cluster_context_note(
        ClusterFacts(provider=info.provider, distribution=info.distribution)
    )
