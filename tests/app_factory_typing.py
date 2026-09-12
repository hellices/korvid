"""Compile-time regressions for the fully wired test app factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.app_factory import build_test_app

    build_test_app(
        unknown_runtime_option=True  # type: ignore[call-arg]  # The factory must reject unknown KorvidApp keywords.
    )
