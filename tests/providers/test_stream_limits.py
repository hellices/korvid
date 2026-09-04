import pytest

from korvid.providers.errors import ProviderError
from korvid.providers.openai_compat import ProviderError as CompatProviderError
from korvid.providers.stream_limits import (
    MAX_PROBE_TEXT_BYTES,
    MAX_REASONING_BYTES,
    MAX_TOOL_ARGUMENTS_BYTES,
    MAX_TOOL_CALLS,
    append_bounded,
    require_count,
)


def test_constants_define_shared_stream_limits() -> None:
    assert MAX_TOOL_CALLS == 64
    assert MAX_TOOL_ARGUMENTS_BYTES == 65_536
    assert MAX_REASONING_BYTES == 262_144
    assert MAX_PROBE_TEXT_BYTES == 16_384


def test_append_bounded_counts_utf8_bytes() -> None:
    with pytest.raises(ProviderError, match="reasoning exceeds"):
        append_bounded("é", "é", max_bytes=3, label="reasoning")


def test_append_bounded_does_not_echo_accumulated_content() -> None:
    with pytest.raises(ProviderError, match="probe text exceeds 3 UTF-8 bytes") as exc_info:
        append_bounded("ab", "cd", max_bytes=3, label="probe text")
    assert "ab" not in str(exc_info.value)
    assert "cd" not in str(exc_info.value)


def test_require_count_rejects_next_item() -> None:
    with pytest.raises(ProviderError, match="tool calls exceeds"):
        require_count(64, max_count=64, label="tool calls")


def test_provider_error_still_imports_from_openai_compat() -> None:
    assert CompatProviderError is ProviderError
