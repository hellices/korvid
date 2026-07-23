from korvid.core.errors import explain_api_error


def test_403_names_the_permission_and_namespace() -> None:
    msg = explain_api_error(403, "Forbidden", "pods", "prod")
    assert "pods" in msg
    assert "prod" in msg
    assert "permission" in msg.lower()


def test_401_suggests_reauth() -> None:
    msg = explain_api_error(401, "Unauthorized", "pods", None)
    assert "credential" in msg.lower() or "re-auth" in msg.lower()


def test_unknown_falls_back_to_reason() -> None:
    msg = explain_api_error(500, "Internal error", "pods", None)
    assert "Internal error" in msg
