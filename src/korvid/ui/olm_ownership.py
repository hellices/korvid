"""Which Subscription this session knows installed a CSV (issue #388).

Deleting an OLM ClusterServiceVersion on its own is not a delete: OLM
reinstalls whatever its Subscription still asks for, so `Ctrl-D` on such a
row is redirected to the full operator uninstall instead. The redirect is
conditional, and the condition is a *store* read - only a Subscription
this session has already watched can name the CSV it installed.

That one condition decides two things which must never disagree: which
flow the keypress actually runs, and which prerequisites the Action
Palette reports for the row. `OperatorController.csv_uninstall_redirect`
answers the first, `WriteAvailability` the second, and neither may import
the other - so the rule they share lives here, below both.

Pure Python: no Textual, no cluster call, no notification, nothing
awaited. That is what lets a palette probe ask it at all (#388 round 14).
"""

from __future__ import annotations

from dataclasses import dataclass

from korvid.core.store import Summary
from korvid.k8s.olm import OPERATORS_GROUP
from korvid.ui.view_state import ViewState


def olm_alias_key(view: ViewState, plural: str) -> str | None:
    """The aliases key resolving to the OLM *plural*, or None.

    Prefers the group-qualified alias, like `resolve_olm_meta`, and checks
    the group rather than the plural: a same-plural CRD from another group
    that won the alias collision is a different resource entirely, and
    "not discovered" is the honest answer for OLM then.

    Args:
        view: The session's read-only view of aliases and loaded rows.
        plural: The OLM resource plural, e.g. `subscriptions`.

    Returns:
        The alias key, or None when this session discovered no such OLM
        resource.
    """
    for key in (f"{plural}.{OPERATORS_GROUP}", plural):
        meta = view.aliases().get(key)
        if meta is not None and meta.group == OPERATORS_GROUP:
            return key
    return None


@dataclass(frozen=True, slots=True)
class SubscriptionOwner:
    """A cached Subscription row that installed a CSV, and its alias key.

    The key travels with the row because the uninstall needs both: the
    `ResourceMeta` behind the alias to address the Subscription, and the
    same key as the kind its manifest is fetched under.
    """

    alias_key: str
    row: Summary

    @property
    def namespace(self) -> str | None:
        """The Subscription's namespace, or None when it carries none."""
        return self.row.namespace or None

    @property
    def name(self) -> str:
        """The Subscription's name."""
        return self.row.name

    @property
    def uid(self) -> str | None:
        """The row's uid, or None - a row loaded without one cannot pin."""
        return str(getattr(self.row, "uid", "") or "") or None


def owning_subscription(view: ViewState, ns: str | None, csv_name: str) -> SubscriptionOwner | None:
    """The watched Subscription whose `status.installedCSV` is *csv_name*.

    Only rows this session has already loaded count: the lookup reads the
    store and never fetches, so a Subscription that exists in the cluster
    but has not been watched is simply not an owner here - and the CSV then
    keeps the ordinary delete, which is exactly what the keypress does.

    Args:
        view: The session's read-only view of aliases and loaded rows.
        ns: The CSV's namespace, or None to accept an owner in any.
        csv_name: The CSV row's name, as `status.installedCSV` spells it.

    Returns:
        The owning Subscription, or None when this session knows of none.
    """
    key = olm_alias_key(view, "subscriptions")
    if key is None:
        return None
    row = next(
        (
            obj
            for obj in view.resources(view.canonical_kind(key), view.current_scope())
            if getattr(obj, "installed_csv", "") == csv_name and (ns is None or obj.namespace == ns)
        ),
        None,
    )
    return None if row is None else SubscriptionOwner(key, row)
