"""Fixed modal bindings that priority app remaps must not intercept."""

from __future__ import annotations

from collections.abc import Iterator

from textual.binding import BindingType
from textual.dom import DOMNode

from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt, ReplicasPrompt
from korvid.ui.widgets.containers_screen import ContainersScreen
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.helm_chart_search import HelmChartSearchScreen
from korvid.ui.widgets.helm_install import ChartReadmeScreen, HelmInstallPrompt
from korvid.ui.widgets.helm_repos import HelmRepoScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.hierarchy_screen import HierarchyScreen
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen
from korvid.ui.widgets.model_search_screen import ModelSearchScreen
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.path_picker import LocalPathPickerScreen, RemotePathPickerScreen
from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.port_forward_screen import ForwardListScreen, PortForwardScreen
from korvid.ui.widgets.profile_manager_screen import ProfileManagerScreen
from korvid.ui.widgets.pulse import PulseScreen
from korvid.ui.widgets.relationship_screen import RelationshipScreen
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.widgets.secret_screen import SecretScreen
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen
from korvid.ui.widgets.telepresence_screen import TelepresenceScreen
from korvid.ui.widgets.transfer_screen import TransferProgressScreen, TransferScreen

_MODAL_SCREEN_TYPES: tuple[type[DOMNode], ...] = (
    ActionPaletteScreen,
    AgentSetupScreen,
    ConfirmScreen,
    ImagePrompt,
    ReplicasPrompt,
    ContainersScreen,
    DescribeScreen,
    HelmChartSearchScreen,
    ChartReadmeScreen,
    HelmInstallPrompt,
    HelmRepoScreen,
    HelpScreen,
    HierarchyScreen,
    HintDetailScreen,
    KeybindingEditorScreen,
    ModelSearchScreen,
    OperatorInstallPrompt,
    LocalPathPickerScreen,
    RemotePathPickerScreen,
    PayloadInspectorScreen,
    PickScreen,
    ForwardListScreen,
    PortForwardScreen,
    ProfileManagerScreen,
    PulseScreen,
    RelationshipScreen,
    ResizePrompt,
    SecretScreen,
    SessionTimelineScreen,
    TelepresenceScreen,
    TransferScreen,
    TransferProgressScreen,
)


def modal_bindings() -> Iterator[BindingType]:
    """Yield current modal declarations, including inherited controls."""
    for modal_type in _MODAL_SCREEN_TYPES:
        for ancestor in reversed(modal_type.__mro__):
            if issubclass(ancestor, DOMNode):
                yield from ancestor.BINDINGS
