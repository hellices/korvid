"""Explicit fully-wired application factory for tests."""

from __future__ import annotations

from typing import Any, TypeVar, overload

from korvid.__main__ import assemble_app_runtime
from korvid.ui.app import KorvidApp

AppT = TypeVar("AppT", bound=KorvidApp)


@overload
def build_test_app(**kwargs: Any) -> KorvidApp: ...


@overload
def build_test_app(app_type: type[AppT], /, **kwargs: Any) -> AppT: ...


def build_test_app(app_type: type[KorvidApp] = KorvidApp, /, **kwargs: Any) -> KorvidApp:
    """Construct an app shell and bind its production runtime graph."""
    return assemble_app_runtime(app_type(**kwargs))
