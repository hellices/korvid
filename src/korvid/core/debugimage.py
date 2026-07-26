"""Runtime-aware debug image recommendation for kubectl debug (issue #52).

Pure heuristics over the pod manifest — no network calls, no probing inside
the target container.  Detection is best-effort: an opaque image name (private
registry digest) falls back to container ports and well-known env vars, and an
unknown runtime simply yields the generic fallback image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Generic fallback when nothing is configured and no runtime is detected.
FALLBACK_IMAGE = "busybox:1.36"

#: De-facto standard network debugging toolkit.
NETSHOOT_IMAGE = "nicolaka/netshoot"

#: Curated per-runtime toolkit images (KoolKits).
KOOLKITS_IMAGES = {
    "jvm": "lightruncom/koolkits:jvm",
    "python": "lightruncom/koolkits:python",
    "nodejs": "lightruncom/koolkits:nodejs",
    "golang": "lightruncom/koolkits:golang",
}

#: What each toolkit brings, for the recommendation reason line.
_RUNTIME_TOOLS = {
    "jvm": "jattach, async-profiler, JDK tools (jcmd/jstack/jmap)",
    "python": "pyflame-style profilers, pdb tooling, pip",
    "nodejs": "node inspector tooling, npm",
    "golang": "delve, go tool pprof",
}

# Image-name patterns per runtime; matched against the whole image reference
# with word-ish boundaries so `gonzo` never matches golang's `go`.
_IMAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "jvm",
        re.compile(
            r"(?:^|[/:._-])(temurin|openjdk|(?:amazon)?corretto|zulu|jre|jdk|java)(?:$|[/:._@-])"
        ),
    ),
    ("python", re.compile(r"(?:^|[/:._-])(python|pypy)(?:$|[/:._@-])")),
    ("nodejs", re.compile(r"(?:^|[/:._-])(node|nodejs)(?:$|[/:._@-])")),
    ("golang", re.compile(r"(?:^|[/:._-])(golang)(?:$|[/:._@-])")),
)

#: Well-known debug/inspector ports.
_PORT_SIGNALS = {
    5005: ("jvm", "container port 5005 (JDWP)"),
    9229: ("nodejs", "container port 9229 (Node inspector)"),
}

#: Well-known runtime env vars.
_ENV_SIGNALS = {
    "JAVA_TOOL_OPTIONS": "jvm",
    "JAVA_OPTS": "jvm",
    "PYTHONPATH": "python",
    "NODE_OPTIONS": "nodejs",
    "GODEBUG": "golang",
}

#: Waiting reasons that mean the ephemeral container image cannot be pulled.
_PULL_FAILURE_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff"})


@dataclass(frozen=True)
class DebugImageOption:
    """One selectable debug image with a human-readable label and reason."""

    image: str
    label: str
    reason: str


def _find_container(manifest: dict[str, Any], container: str | None) -> dict[str, Any] | None:
    spec = manifest.get("spec") or {}
    containers: list[dict[str, Any]] = spec.get("containers") or []
    if not containers:
        return None
    if container is None:
        return containers[0]
    for entry in containers:
        if entry.get("name") == container:
            return entry
    return None


def detect_runtime(manifest: dict[str, Any], container: str | None) -> tuple[str, str] | None:
    """Best-effort runtime detection for the target container.

    Returns `(runtime, reason)` — e.g. `("jvm", "image name 'openjdk:17'")` —
    or `None` when nothing matches.  Signals in priority order: image name,
    then well-known ports, then env vars.  Never probes the cluster.
    """
    entry = _find_container(manifest, container)
    if entry is None:
        return None

    image = str(entry.get("image") or "")
    for runtime, pattern in _IMAGE_PATTERNS:
        if pattern.search(image):
            return (runtime, f"image name {image!r}")

    for port_entry in entry.get("ports") or []:
        port = port_entry.get("containerPort")
        if port in _PORT_SIGNALS:
            runtime, reason = _PORT_SIGNALS[port]
            return (runtime, reason)

    for env_entry in entry.get("env") or []:
        runtime_match = _ENV_SIGNALS.get(str(env_entry.get("name") or ""))
        if runtime_match:
            return (runtime_match, f"env var {env_entry.get('name')}")

    return None


def recommend_debug_images(
    manifest: dict[str, Any],
    container: str | None,
    *,
    images_cfg: dict[str, str] | None = None,
    default_image: str | None = None,
) -> list[DebugImageOption]:
    """Ordered debug-image options for the kubectl debug offer dialog.

    Zero config: a detected runtime leads with its KoolKits toolkit, then
    netshoot for network debugging, then the busybox fallback.  When
    `images_cfg` is set (air-gapped / private registry), only configured
    images are offered — public registry access is never assumed.
    """
    fallback = default_image or FALLBACK_IMAGE
    detected = detect_runtime(manifest, container)

    options: list[DebugImageOption] = []

    def _add(image: str, label: str, reason: str) -> None:
        if any(existing.image == image for existing in options):
            return
        options.append(DebugImageOption(image=image, label=label, reason=reason))

    if images_cfg:
        if detected is not None:
            runtime, signal = detected
            configured = images_cfg.get(runtime)
            if configured:
                _add(
                    configured, f"{runtime} toolkit (configured)", f"detected {runtime} — {signal}"
                )
        _add(fallback, "default (configured)", "configured fallback image")
        return options

    if detected is not None:
        runtime, signal = detected
        _add(
            KOOLKITS_IMAGES[runtime],
            f"{runtime} toolkit (koolkits)",
            f"detected {runtime} runtime ({signal}) — includes {_RUNTIME_TOOLS[runtime]}",
        )
        _add(NETSHOOT_IMAGE, "network toolkit (netshoot)", "network debugging tools")
        _add(fallback, "minimal (busybox)", "lowest-common-denominator shell")
        return options

    _add(fallback, "minimal (busybox)", "no runtime detected — generic shell")
    _add(NETSHOOT_IMAGE, "network toolkit (netshoot)", "network debugging tools")
    return options


def find_pull_failure(manifest: dict[str, Any], image: str) -> str | None:
    """Return the pull-failure reason for `image`'s ephemeral container, if any.

    Scans `status.ephemeralContainerStatuses` for a waiting state with
    `ErrImagePull`/`ImagePullBackOff` on a container running exactly `image`.
    Matching on the image keeps older failed ephemeral containers (which can
    never be removed from the pod spec) from triggering a false fallback.
    """
    status = manifest.get("status") or {}
    for entry in status.get("ephemeralContainerStatuses") or []:
        if entry.get("image") != image:
            continue
        waiting = (entry.get("state") or {}).get("waiting") or {}
        reason = waiting.get("reason")
        if reason in _PULL_FAILURE_REASONS:
            return str(reason)
    return None
