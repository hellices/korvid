from korvid.providers.static_creds import StaticHeaderSource


async def test_static_bearer_header() -> None:
    src = StaticHeaderSource("sk-1")
    assert await src.headers() == {"Authorization": "Bearer sk-1"}


async def test_static_custom_header_no_prefix() -> None:
    src = StaticHeaderSource("k", header="api-key", prefix="")
    assert await src.headers() == {"api-key": "k"}


async def test_aclose_is_no_op() -> None:
    src = StaticHeaderSource("sk-1")
    await src.aclose()  # should not raise
