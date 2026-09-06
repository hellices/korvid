from korvid.k8s.discovery import ResourceMeta
from korvid.ui.command import command_words
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.namespace_picker import NamespacePicker
from tests.ui.test_app import make_app
from tests.ui.waits import until


async def test_app_completion_uses_catalog_at_startup_and_after_discovery() -> None:
    app = make_app([])
    async with app.run_test():
        bar = app.query_one(CommandBar)
        assert bar.command_words == command_words(app.aliases)
        app.aliases["widgets.example.io"] = ResourceMeta(
            "Widget", "widgets", "example.io", "v1", True
        )
        app.on_aliases_updated()
        assert bar.command_words == command_words(app.aliases)
        assert bar.complete("widgets.") == "widgets.example.io"


async def test_namespace_picker_preserves_the_resource_view() -> None:
    app = make_app([])
    async with app.run_test() as pilot:
        await app._workspace_ctl.navigate("deployments", "default")
        picker = app.query_one(NamespacePicker)
        picker.open(["team-a"])
        await pilot.press("enter")
        await until(pilot, lambda: app.current_scope == "team-a", label="namespace changed")
        assert app.current_kind == "deployments"
