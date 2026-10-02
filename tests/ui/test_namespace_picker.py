"""The `:ns` picker labels slot numbers from the dispatch map (issue #406)."""

from __future__ import annotations

from textual.app import App, ComposeResult

from korvid.core.namespace_slots import SlotEntry, SlotMap, SlotOrigin
from korvid.ui.messages import NavigateCommand
from korvid.ui.widgets.namespace_picker import NamespacePicker
from tests.ui.waits import until


class _PickerApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.navigation: NavigateCommand | None = None

    def compose(self) -> ComposeResult:
        yield NamespacePicker()

    def on_navigate_command(self, message: NavigateCommand) -> None:
        self.navigation = message


SLOTS = SlotMap(
    {
        1: SlotEntry("prod", SlotOrigin.PINNED),
        2: SlotEntry("dev", SlotOrigin.AUTO),
        3: SlotEntry("gone", SlotOrigin.AUTO, available=False),
    }
)


def _rows(picker: NamespacePicker) -> list[tuple[str | None, str, bool]]:
    return [
        (option.id, str(option.prompt), option.disabled)
        for option in (picker.get_option_at_index(i) for i in range(picker.option_count))
    ]


async def test_listed_namespaces_carry_their_slot_and_unavailable_slots_are_disabled() -> None:
    app = _PickerApp()
    async with app.run_test():
        picker = app.query_one(NamespacePicker)
        picker.open(["dev", "prod", "zeta"], SLOTS)

        assert _rows(picker) == [
            ("dev", "2  dev", False),
            ("prod", "1  prod", False),
            ("zeta", "   zeta", False),
            ("gone", "3  gone (unavailable)", True),
        ]


async def test_selection_navigates_by_the_option_id_not_its_label() -> None:
    app = _PickerApp()
    async with app.run_test() as pilot:
        app.query_one(NamespacePicker).open(["dev"], SLOTS)
        await pilot.press("enter")

        await until(pilot, lambda: app.navigation is not None, label="namespace selected")
        assert app.navigation is not None
        assert app.navigation.namespace == "dev"


async def test_without_slots_the_picker_lists_plain_names() -> None:
    app = _PickerApp()
    async with app.run_test():
        picker = app.query_one(NamespacePicker)
        picker.open(["dev"])

        assert _rows(picker) == [("dev", "dev", False)]
