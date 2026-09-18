"""Explicit fully-wired application factory for tests."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Iterator
from typing import Any, ParamSpec, TypeVar
from unittest import mock

from korvid.__main__ import assemble_app_runtime
from korvid.ui.app import KorvidApp

AppT = TypeVar("AppT", bound=KorvidApp)
AppP = ParamSpec("AppP")


@contextlib.contextmanager
def session_kubectl(present: bool) -> Iterator[None]:
    """Decide the `kubectl` snapshot of every app built inside this block.

    `KubectlPresence` resolves while the runtime is assembled (#388 round
    14), so a harness that needs a session with - or without - `kubectl`
    has to decide it around `build_test_app`, not around the keypress it is
    exercising. Left to the real PATH the answer would be the runner's,
    which is how a shell or forward test would start passing or failing on
    whether CI happens to ship the binary.

    Only `kubectl` is answered: every other lookup falls through to the
    real `shutil.which`, so this cannot silently invent a `helm` or a
    `telepresence` for a session that was composed without one.
    """
    real_which = shutil.which

    def which(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if cmd == "kubectl":
            return "/usr/bin/kubectl" if present else None
        return real_which(cmd, *args, **kwargs)

    with mock.patch("shutil.which", side_effect=which):
        yield


def _bind_test_app_factory(app_type: Callable[AppP, AppT], /) -> Callable[AppP, AppT]:
    """Bind a fully typed constructor to the runtime assembler."""

    def build(*args: AppP.args, **kwargs: AppP.kwargs) -> AppT:
        return assemble_app_runtime(app_type(*args, **kwargs))

    return build


build_test_app = _bind_test_app_factory(KorvidApp)


def build_test_subclass(
    app_type: Callable[AppP, AppT],
    /,
    *args: AppP.args,
    **kwargs: AppP.kwargs,
) -> AppT:
    """Construct and fully wire an explicitly selected app subclass."""
    return assemble_app_runtime(app_type(*args, **kwargs))
