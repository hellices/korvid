"""Tests for runtime-aware debug image recommendation (issue #52)."""

from __future__ import annotations

from typing import Any

from korvid.core.debugimage import (
    DebugImageOption,
    detect_runtime,
    ephemeral_container_names,
    find_pull_failure,
    recommend_debug_images,
)


def _pod(
    *,
    image: str = "registry.local/app@sha256:abcd",
    ports: list[int] | None = None,
    env: list[str] | None = None,
    container: str = "app",
) -> dict[str, Any]:
    return {
        "spec": {
            "containers": [
                {
                    "name": container,
                    "image": image,
                    "ports": [{"containerPort": p} for p in (ports or [])],
                    "env": [{"name": n, "value": "x"} for n in (env or [])],
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# detect_runtime: image-name patterns
# ---------------------------------------------------------------------------


def test_detect_runtime_jvm_image_names() -> None:
    for image in (
        "eclipse-temurin:21-jre",
        "openjdk:17",
        "amazoncorretto:21",
        "azul/zulu-openjdk:21",
        "myregistry.io/team/service-jdk:1.2",
    ):
        detected = detect_runtime(_pod(image=image), "app")
        assert detected is not None, image
        runtime, reason = detected
        assert runtime == "jvm"
        assert "image" in reason


def test_detect_runtime_python_node_go_image_names() -> None:
    cases = {
        "python:3.12-slim": "python",
        "docker.io/library/node:22-alpine": "nodejs",
        "golang:1.23": "golang",
        "go:1.23": "golang",
        "docker.io/library/go:1.23": "golang",
    }
    for image, expected in cases.items():
        detected = detect_runtime(_pod(image=image), "app")
        assert detected is not None, image
        assert detected[0] == expected


def test_detect_runtime_unknown_image_returns_none() -> None:
    assert detect_runtime(_pod(image="registry.local/app@sha256:abcd"), "app") is None


def test_detect_runtime_matches_requested_container_only() -> None:
    manifest = {
        "spec": {
            "containers": [
                {"name": "app", "image": "registry.local/opaque@sha256:ff"},
                {"name": "sidecar", "image": "python:3.12"},
            ]
        }
    }
    assert detect_runtime(manifest, "app") is None
    detected = detect_runtime(manifest, "sidecar")
    assert detected is not None
    assert detected[0] == "python"


def test_detect_runtime_defaults_to_first_container() -> None:
    detected = detect_runtime(_pod(image="node:22"), None)
    assert detected is not None
    assert detected[0] == "nodejs"


def test_detect_runtime_word_boundary_avoids_false_positives() -> None:
    # "gonzo" must not match golang's "go"; "mongo" ends in "go" but is bounded.
    assert detect_runtime(_pod(image="registry.local/gonzo:1"), "app") is None
    assert detect_runtime(_pod(image="mongo:7"), "app") is None


# ---------------------------------------------------------------------------
# detect_runtime: secondary signals (ports, env) for opaque image names
# ---------------------------------------------------------------------------


def test_detect_runtime_jdwp_port_signals_jvm() -> None:
    detected = detect_runtime(_pod(ports=[8080, 5005]), "app")
    assert detected is not None
    runtime, reason = detected
    assert runtime == "jvm"
    assert "5005" in reason


def test_detect_runtime_node_inspector_port() -> None:
    detected = detect_runtime(_pod(ports=[9229]), "app")
    assert detected is not None
    assert detected[0] == "nodejs"


def test_detect_runtime_env_var_signals() -> None:
    cases = {
        "JAVA_TOOL_OPTIONS": "jvm",
        "PYTHONPATH": "python",
        "NODE_OPTIONS": "nodejs",
        "GODEBUG": "golang",
    }
    for env_name, expected in cases.items():
        detected = detect_runtime(_pod(env=[env_name]), "app")
        assert detected is not None, env_name
        runtime, reason = detected
        assert runtime == expected
        assert env_name in reason


def test_detect_runtime_image_name_wins_over_env() -> None:
    detected = detect_runtime(_pod(image="python:3.12", env=["NODE_OPTIONS"]), "app")
    assert detected is not None
    assert detected[0] == "python"


# ---------------------------------------------------------------------------
# recommend_debug_images
# ---------------------------------------------------------------------------


def test_recommend_detected_runtime_leads_with_koolkits() -> None:
    options = recommend_debug_images(_pod(image="openjdk:17"), "app")
    assert options[0].image == "lightruncom/koolkits:jvm"
    assert "jvm" in options[0].reason.lower() or "JVM" in options[0].reason
    images = [o.image for o in options]
    assert "nicolaka/netshoot" in images
    assert "busybox:1.36" in images


def test_recommend_unknown_runtime_defaults_to_fallback_first() -> None:
    options = recommend_debug_images(_pod(), "app")
    assert options[0].image == "busybox:1.36"
    assert "nicolaka/netshoot" in [o.image for o in options]


def test_recommend_with_images_config_uses_only_configured() -> None:
    options = recommend_debug_images(
        _pod(image="openjdk:17"),
        "app",
        images_cfg={"jvm": "registry.corp.local/tools/debug-jvm:1"},
        default_image="registry.corp.local/tools/busybox:1.36",
    )
    images = [o.image for o in options]
    assert images[0] == "registry.corp.local/tools/debug-jvm:1"
    assert "registry.corp.local/tools/busybox:1.36" in images
    # Air-gapped: never assume public registry access.
    assert "lightruncom/koolkits:jvm" not in images
    assert "nicolaka/netshoot" not in images


def test_recommend_images_config_without_detected_runtime() -> None:
    options = recommend_debug_images(
        _pod(),
        "app",
        images_cfg={"jvm": "registry.corp.local/tools/debug-jvm:1"},
        default_image="registry.corp.local/tools/busybox:1.36",
    )
    # Undetected runtime: only the configured default is offered.
    assert [o.image for o in options] == ["registry.corp.local/tools/busybox:1.36"]


def test_recommend_images_config_without_default_never_offers_public_fallback() -> None:
    """debug.images without debug.default_image must not leak busybox from a
    public registry into an air-gapped picker (issue #52 review)."""
    options = recommend_debug_images(
        _pod(image="openjdk:17"),
        "app",
        images_cfg={"jvm": "registry.corp.local/tools/debug-jvm:1"},
    )
    assert [o.image for o in options] == ["registry.corp.local/tools/debug-jvm:1"]


def test_recommend_images_config_without_default_or_match_returns_empty() -> None:
    """Configured images, no default, no runtime match: nothing to offer —
    the UI falls back to the custom-image prompt only."""
    options = recommend_debug_images(
        _pod(),
        "app",
        images_cfg={"jvm": "registry.corp.local/tools/debug-jvm:1"},
    )
    assert options == []


def test_recommend_default_image_overrides_busybox() -> None:
    options = recommend_debug_images(
        _pod(), "app", default_image="registry.corp.local/busybox:1.36"
    )
    assert options[0].image == "registry.corp.local/busybox:1.36"
    assert "busybox:1.36" not in [o.image for o in options if o.image.startswith("busybox")]


def test_recommend_node_runtime_uses_koolkits_node_tag() -> None:
    # The official KoolKits Node.js image tag is `node`, not `nodejs`.
    options = recommend_debug_images(_pod(image="node:22"), "app")
    assert options[0].image == "lightruncom/koolkits:node"


def test_recommend_options_have_labels_and_reasons() -> None:
    for option in recommend_debug_images(_pod(image="node:22"), "app"):
        assert isinstance(option, DebugImageOption)
        assert option.label
        assert option.reason


def test_recommend_options_deduplicate_images() -> None:
    # Configured jvm image identical to default must not produce two rows.
    options = recommend_debug_images(
        _pod(image="openjdk:17"),
        "app",
        images_cfg={"jvm": "registry.corp.local/tools/debug:1"},
        default_image="registry.corp.local/tools/debug:1",
    )
    images = [o.image for o in options]
    assert len(images) == len(set(images))


# ---------------------------------------------------------------------------
# find_pull_failure
# ---------------------------------------------------------------------------


def _pod_with_ephemeral_status(image: str, reason: str | None) -> dict[str, Any]:
    waiting = {"reason": reason, "message": f"failed to pull {image}"} if reason else None
    state: dict[str, Any] = {"waiting": waiting} if waiting else {"running": {}}
    return {
        "status": {
            "ephemeralContainerStatuses": [{"name": "debugger-abc", "image": image, "state": state}]
        }
    }


def test_find_pull_failure_err_image_pull() -> None:
    manifest = _pod_with_ephemeral_status("lightruncom/koolkits:jvm", "ErrImagePull")
    reason = find_pull_failure(manifest, "lightruncom/koolkits:jvm")
    assert reason == "ErrImagePull"


def test_find_pull_failure_image_pull_backoff() -> None:
    manifest = _pod_with_ephemeral_status("nicolaka/netshoot", "ImagePullBackOff")
    assert find_pull_failure(manifest, "nicolaka/netshoot") == "ImagePullBackOff"


def test_find_pull_failure_running_container_is_none() -> None:
    manifest = _pod_with_ephemeral_status("busybox:1.36", None)
    assert find_pull_failure(manifest, "busybox:1.36") is None


def test_find_pull_failure_other_waiting_reason_is_none() -> None:
    manifest = _pod_with_ephemeral_status("busybox:1.36", "ContainerCreating")
    assert find_pull_failure(manifest, "busybox:1.36") is None


def test_find_pull_failure_matches_image() -> None:
    # An older failed ephemeral container for a different image must not
    # trigger the fallback for the current attach.
    manifest = _pod_with_ephemeral_status("lightruncom/koolkits:jvm", "ErrImagePull")
    assert find_pull_failure(manifest, "busybox:1.36") is None


def test_find_pull_failure_no_statuses() -> None:
    assert find_pull_failure({}, "busybox:1.36") is None


def test_find_pull_failure_ignores_preexisting_containers() -> None:
    # A stale failed entry with the SAME image (left over from an earlier
    # attempt - ephemeral containers cannot be removed) must not be blamed
    # on the current attach when its name was snapshotted beforehand.
    manifest = _pod_with_ephemeral_status("busybox:1.36", "ErrImagePull")
    assert find_pull_failure(manifest, "busybox:1.36", ignore=frozenset({"debugger-abc"})) is None
    # A new (non-ignored) entry still reports the failure.
    assert find_pull_failure(manifest, "busybox:1.36", ignore=frozenset({"other"})) == (
        "ErrImagePull"
    )


def test_ephemeral_container_names() -> None:
    manifest = _pod_with_ephemeral_status("busybox:1.36", "ErrImagePull")
    assert ephemeral_container_names(manifest) == frozenset({"debugger-abc"})
    assert ephemeral_container_names({}) == frozenset()
