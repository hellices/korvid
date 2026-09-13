"""Explicit fully-wired application factory for tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import ParamSpec, TypeVar

from korvid.__main__ import assemble_app_runtime
from korvid.ui.app import KorvidApp

AppT = TypeVar("AppT", bound=KorvidApp)
AppP = ParamSpec("AppP")


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
