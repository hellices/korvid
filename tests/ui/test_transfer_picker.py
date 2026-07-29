"""Tests for the ctrl+o path pickers in the transfer dialog (issue #124)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import DirectoryTree, Input, OptionList, RadioSet, Static

from korvid.core.transfer import TransferError
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.path_picker import LocalPathPickerScreen, RemotePathPickerScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.transfer_screen import TransferScreen
from tests.ui.test_app import make_app
from tests.ui.test_transfer import SUCCESS, FakeExecOpener, _dialog, _pod
from tests.ui.waits import until

NOT_FOUND = json.dumps(
    {
        "metadata": {},
        "status": "Failure",
        "message": 'exec: "ls": executable file not found in $PATH',
        "reason": "InternalError",
    }
).encode()


def _listing(*names: str) -> list[bytes]:
    payload = ("\n".join(names) + "\n").encode() if names else b""
    frames = [b"\x03" + SUCCESS]
    if payload:
        frames.insert(0, b"\x01" + payload)
    return frames


async def _open_dialog(pilot: Pilot[Any], app: KorvidApp) -> TransferScreen:
    await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
    await pilot.press("ctrl+t")
    await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
    return _dialog(app)


class TestLocalPicker:
    async def test_ctrl_o_on_local_input_opens_directory_tree(self, tmp_path: Path) -> None:
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            local = dialog.query_one("#transfer-local", Input)
            local.value = str(tmp_path / "app.log")
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            tree = app.screen.query_one(DirectoryTree)
            # Rooted at the deepest existing directory of the input value.
            assert Path(tree.path) == tmp_path

    async def test_selecting_file_fills_local_input(self, tmp_path: Path) -> None:
        (tmp_path / "existing.log").write_bytes(b"x")
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            local = dialog.query_one("#transfer-local", Input)
            local.value = str(tmp_path) + "/"
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            tree = app.screen.query_one(DirectoryTree)
            await until(pilot, lambda: len(tree.root.children) == 1, label="tree loaded")
            tree.focus()
            await pilot.press("down", "enter")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert local.value == str(tmp_path / "existing.log")

    async def test_s_selects_directory_and_appends_remote_basename(self, tmp_path: Path) -> None:
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            dialog.query_one("#transfer-remote", Input).value = "/var/log/app.log"
            local = dialog.query_one("#transfer-local", Input)
            local.value = str(tmp_path) + "/"
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            app.screen.query_one(DirectoryTree).focus()
            await pilot.press("s")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert local.value == str(tmp_path / "app.log")

    async def test_escape_keeps_input_unchanged(self, tmp_path: Path) -> None:
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            local = dialog.query_one("#transfer-local", Input)
            local.value = str(tmp_path / "keep.log")
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert local.value == str(tmp_path / "keep.log")


class TestRemotePicker:
    async def test_ctrl_o_on_remote_input_lists_directory(self) -> None:
        opener = FakeExecOpener(_listing("config/", "app.log"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/app.log"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            # One exec round-trip listing the input value's directory.
            assert opener.calls[0]["command"] == ["ls", "-1Ap", "--", "/srv/"]
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 3, label="options")
            prompts = [str(options.get_option_at_index(i).prompt) for i in range(3)]
            assert prompts == ["../", "config/", "app.log"]

    async def test_selecting_file_fills_remote_input(self) -> None:
        opener = FakeExecOpener(_listing("app.log"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            options.focus()
            await pilot.press("down", "enter")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert remote.value == "/srv/app.log"

    async def test_entering_directory_lists_it(self) -> None:
        opener = FakeExecOpener(_listing("config/"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            options.focus()
            await pilot.press("down", "enter")
            await until(pilot, lambda: len(opener.calls) == 2, label="second listing")
            assert opener.calls[1]["command"] == ["ls", "-1Ap", "--", "/srv/config/"]
            assert isinstance(app.screen, RemotePathPickerScreen)

    async def test_s_fills_current_directory_with_upload_basename(self, tmp_path: Path) -> None:
        opener = FakeExecOpener(_listing("config/"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            dialog.select_upload()
            dialog.query_one("#transfer-local", Input).value = str(tmp_path / "bundle.tgz")
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            options.focus()
            await pilot.press("s")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert remote.value == "/srv/bundle.tgz"

    async def test_listing_unavailable_degrades_to_manual_entry(self) -> None:
        # Distroless: no ls in the image. The picker never opens usefully —
        # a toast explains, and the dialog keeps working as before.
        opener = FakeExecOpener([b"\x03" + NOT_FOUND])
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/app.log"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot,
                lambda: any(
                    "enter the path manually" in str(n.message) for n in app._notifications
                ),
                label="degradation toast",
            )
            await until(pilot, lambda: app.screen is dialog, label="dialog kept")
            assert remote.value == "/srv/app.log"


class TestBrowseGating:
    async def test_ctrl_o_outside_path_fields_does_nothing(self) -> None:
        # ctrl+o is a screen binding: with the direction radio focused it
        # must not open any picker.
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            dialog.query_one(RadioSet).focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert app.screen is dialog

    async def test_unexpandable_tilde_falls_back_to_home(self) -> None:
        # Path.expanduser raises RuntimeError for "~no_such_user/f": browsing
        # must fall back to home, not escape the dialog handler.
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            local = dialog.query_one("#transfer-local", Input)
            local.value = "~no_such_user_hopefully/file.log"
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            tree = app.screen.query_one(DirectoryTree)
            assert Path(tree.path) == Path("~").expanduser()


class TestRemotePickerRobustness:
    async def test_escape_dismisses_while_listing_stalls(self) -> None:
        # A stalled exec connection must not turn the convenience picker
        # into a gate: the listing runs in a cancellable worker, so Esc
        # still dismisses the picker while the listing hangs.
        release = asyncio.Event()

        class StallingOpener:
            def __call__(
                self,
                namespace: str,
                pod: str,
                container: str | None,
                command: list[str],
                *,
                stdin: bool,
            ) -> contextlib.AbstractAsyncContextManager[Any]:
                @contextlib.asynccontextmanager
                async def _cm() -> AsyncIterator[Any]:
                    await release.wait()
                    yield FakeWsEmpty()

                return _cm()

        class FakeWsEmpty:
            def __aiter__(self) -> FakeWsEmpty:
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

        app = make_app([_pod("api-1")], open_pod_exec=StallingOpener())
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            try:
                # Pre-fix, the blocked screen pump hangs the pilot itself:
                # bound the interaction so the regression fails, not hangs.
                async with asyncio.timeout(10):
                    remote = dialog.query_one("#transfer-remote", Input)
                    remote.value = "/srv/"
                    remote.focus()
                    await pilot.press("ctrl+o")
                    await until(
                        pilot,
                        lambda: isinstance(app.screen, RemotePathPickerScreen),
                        label="picker",
                    )
                    await pilot.press("escape")
                    await until(pilot, lambda: app.screen is dialog, label="picker closed")
                assert app.screen is dialog
            finally:
                # Unblock the stalled listing so app teardown can finish even
                # when the timeout above fired (the pre-fix behavior).
                release.set()

    async def test_o_forces_open_symlinked_directory(self) -> None:
        # ls -p marks only real directories: a symlink to a directory shows
        # bare. `o` on the highlighted entry opens it as a directory anyway.
        opener = FakeExecOpener(_listing("link"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            options.focus()
            await pilot.press("down", "o")
            await until(pilot, lambda: len(opener.calls) == 2, label="forced listing")
            assert opener.calls[1]["command"] == ["ls", "-1Ap", "--", "/srv/link/"]
            assert isinstance(app.screen, RemotePathPickerScreen)


class TestContextEpochGuard:
    """Issue #124 review: the lister must be bound to the dialog's context
    epoch — a :ctx switch retargets the shared exec client, so a stale
    lister would browse (and display) the wrong cluster."""

    async def test_ctrl_o_after_context_switch_degrades_without_exec(self) -> None:
        opener = FakeExecOpener(_listing("etc/"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.focus()
            app._ctx_epoch += 1  # a :ctx switch completed under the dialog
            await pilot.press("ctrl+o")
            await until(
                pilot,
                lambda: any("context changed" in str(n.message) for n in app._notifications),
                label="epoch toast",
            )
            await until(pilot, lambda: app.screen is dialog, label="dialog kept")
            assert opener.calls == []  # never touched the (new) cluster

    async def test_result_discarded_when_epoch_changes_during_listing(self) -> None:
        inner = FakeExecOpener(_listing("etc/"))

        def flipping(
            namespace: str,
            pod: str,
            container: str | None,
            command: list[str],
            *,
            stdin: bool,
        ) -> contextlib.AbstractAsyncContextManager[Any]:
            # A switch completes while the listing is in flight.
            app._ctx_epoch += 1
            return inner(namespace, pod, container, command, stdin=stdin)

        app = make_app([_pod("api-1")], open_pod_exec=flipping)
        async with app.run_test():
            lister = app._remote_lister("default", "api-1", "app", uid=None, epoch=app._ctx_epoch)
            assert lister is not None
            with pytest.raises(TransferError, match="context changed"):
                await lister("/")


class TestPodUidGuard:
    """Issue #124 review: a same-named replacement pod does not change the
    context epoch, so the lister must also be bound to the pod uid captured
    when the dialog opened — like the transfer itself (ui/transfer.py)."""

    async def test_ctrl_o_degrades_when_pod_replaced(self) -> None:
        opener = FakeExecOpener(_listing("etc/"))

        async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
            return {"metadata": {"uid": "uid-replacement"}}

        app = make_app(
            [_pod("api-1", uid="uid-approved")],
            open_pod_exec=opener,
            get_manifest=get_manifest,
        )
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot,
                lambda: any("replaced" in str(n.message) for n in app._notifications),
                label="replaced toast",
            )
            await until(pilot, lambda: app.screen is dialog, label="dialog kept")
            assert opener.calls == []  # never listed the replacement pod

    async def test_result_discarded_when_pod_replaced_during_listing(self) -> None:
        uids = ["uid-approved", "uid-replacement"]

        async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
            return {"metadata": {"uid": uids.pop(0)}}

        app = make_app(
            [_pod("api-1", uid="uid-approved")],
            open_pod_exec=FakeExecOpener(_listing("etc/")),
            get_manifest=get_manifest,
        )
        async with app.run_test():
            lister = app._remote_lister(
                "default", "api-1", "app", uid="uid-approved", epoch=app._ctx_epoch
            )
            assert lister is not None
            with pytest.raises(TransferError, match="replaced"):
                await lister("/")

    async def test_no_exec_when_switch_completes_during_uid_lookup(self) -> None:
        # TOCTOU: the epoch is checked before the uid lookup awaits the
        # manifest; a switch completing during that await must be caught
        # again *before* the exec, or the ls runs against the new cluster.
        opener = FakeExecOpener(_listing("etc/"))

        async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
            app._ctx_epoch += 1  # switch completes while the lookup is in flight
            return {"metadata": {"uid": "uid-approved"}}

        app = make_app(
            [_pod("api-1", uid="uid-approved")],
            open_pod_exec=opener,
            get_manifest=get_manifest,
        )
        async with app.run_test():
            lister = app._remote_lister(
                "default", "api-1", "app", uid="uid-approved", epoch=app._ctx_epoch
            )
            assert lister is not None
            with pytest.raises(TransferError, match="context changed"):
                await lister("/")
            assert opener.calls == []  # the guard fired before any exec


class TestWhitespaceVerbatim:
    """Round 8: basename derivation and start-dir probing must also use the
    field values verbatim — "report" and "report " are different files."""

    async def test_local_dir_pick_appends_remote_basename_verbatim(self, tmp_path: Path) -> None:
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            dialog.query_one("#transfer-remote", Input).value = "/srv/report "
            local = dialog.query_one("#transfer-local", Input)
            local.value = str(tmp_path) + "/"
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            app.screen.query_one(DirectoryTree).focus()
            await pilot.press("s")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert local.value == str(tmp_path) + "/report "

    async def test_remote_dir_pick_appends_local_basename_verbatim(self, tmp_path: Path) -> None:
        opener = FakeExecOpener(_listing("config/"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            dialog.select_upload()
            dialog.query_one("#transfer-local", Input).value = str(tmp_path / "bundle ")
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            options.focus()
            await pilot.press("s")
            await until(pilot, lambda: app.screen is dialog, label="picker closed")
            assert remote.value == "/srv/bundle "

    async def test_local_start_dir_with_trailing_space_probed_verbatim(
        self, tmp_path: Path
    ) -> None:
        # An existing directory named "archive " must be probed as-is:
        # stripping probed "archive" (missing) and opened the parent.
        spaced = tmp_path / "archive "
        spaced.mkdir()
        app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener(_listing()))
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            local = dialog.query_one("#transfer-local", Input)
            local.value = str(spaced)
            local.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, LocalPathPickerScreen), label="picker"
            )
            tree = app.screen.query_one(DirectoryTree)
            assert Path(tree.path) == spaced


class TestMarkupSafety:
    """Remote filenames are cluster-controlled: rendering them as Textual
    markup would let "[red]secret[/red]" display as "secret", so the picker
    could transfer a path other than the one the user sees."""

    async def test_markup_in_filenames_rendered_literally(self) -> None:
        opener = FakeExecOpener(_listing("[red]secret[/red]"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            prompt = options.get_option_at_index(1).prompt
            assert isinstance(prompt, Text)
            assert prompt.plain == "[red]secret[/red]"

    async def test_markup_in_directory_path_title_rendered_literally(self) -> None:
        # The picker title embeds directory names picked from the listing;
        # the Static is markup-disabled so brackets stay literal on screen.
        opener = FakeExecOpener(_listing("[red]dir[/red]/"))
        app = make_app([_pod("api-1")], open_pod_exec=opener)
        async with app.run_test() as pilot:
            dialog = await _open_dialog(pilot, app)
            remote = dialog.query_one("#transfer-remote", Input)
            remote.value = "/srv/"
            remote.focus()
            await pilot.press("ctrl+o")
            await until(
                pilot, lambda: isinstance(app.screen, RemotePathPickerScreen), label="picker"
            )
            options = app.screen.query_one(OptionList)
            await until(pilot, lambda: options.option_count == 2, label="options")
            options.focus()
            await pilot.press("down", "enter")  # descend into the marked-up dir
            await until(pilot, lambda: len(opener.calls) == 2, label="descended")
            title = app.screen.query_one(".picker-title", Static)
            assert isinstance(title.visual, Content)
            assert title.visual.plain == "Remote: /srv/[red]dir[/red]"
