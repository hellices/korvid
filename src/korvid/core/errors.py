"""Map raw API errors to actionable, human-readable messages (§5-5)."""

from __future__ import annotations


def explain_api_error(status: int, reason: str, resource: str, namespace: str | None) -> str:
    ns_part = f" (namespace: {namespace})" if namespace else ""
    if status == 403:
        return f"No permission to access {resource}{ns_part}. Check your RBAC role bindings."
    if status == 401:
        return (
            "Credentials expired or invalid — re-authenticate with your cluster (e.g. renew token)."
        )
    return f"API error {status} on {resource}{ns_part}: {reason}"
