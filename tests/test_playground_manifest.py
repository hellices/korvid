"""Contracts for the throwaway-cluster workloads that Getting Started applies.

The guide tells a newcomer to apply a downloaded manifest to a cluster, so what
that manifest creates is a promise: only the `playground` namespace and its
three small workloads, nothing that needs the host or the Kubernetes API, and
exactly the healthy and broken states the guide's table asks the reader to
find. The guide's commands are a promise too: they must not be able to reach
any cluster other than the throwaway one.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tests.platforms import posix_only

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "deploy" / "playground" / "workloads.yaml"
GUIDE = ROOT / "docs" / "getting-started.md"
PRACTICE_HEADING = "## Practice on a throwaway cluster"
RAW_URL = "https://raw.githubusercontent.com/hellices/korvid/main/deploy/playground/workloads.yaml"
NAMESPACE = "playground"
CLUSTER = "korvid-playground"
CONTEXT = f"kind-{CLUSTER}"
DEDICATED_KUBECONFIG = f'export KUBECONFIG="$PWD/{CLUSTER}.kubeconfig"'
#: The guide works in a directory nobody else uses: the download cannot
#: overwrite a reader's own `workloads.yaml`, the kubeconfig file is new, so
#: it holds no other context, and the cleanup removes only that directory.
SCRATCH_DIRECTORY = ('playground="$(mktemp -d)" &&', 'cd "$playground" &&')

#: SHA-256 of the practice section as reviewed in #447. Readers paste these
#: commands into a terminal, so like the reviewed README media, any edit, even
#: to prose, is a deliberate change: re-check that every command still reaches
#: only the throwaway cluster (the tests below encode what to check), then
#: update this value. The section is read with universal newlines, so a CRLF
#: checkout hashes the same.
REVIEWED_SECTION_SHA256 = "c9b42dd590861dbb56921e2c10994dc25a2a40f5da0c1bb7a7e3890252b3807a"

#: kubectl flags and commands that point it somewhere other than the dedicated
#: KUBECONFIG's single context.
RETARGETING = ("--kubeconfig", "--server", "--cluster", "use-context")

#: The only pod fields the workloads may set. An allowlist rather than a
#: denylist, so a new field that grants host access (`hostNetwork`, an init
#: container, a pod `securityContext`) fails here instead of slipping by.
ALLOWED_POD_FIELDS = {"automountServiceAccountToken", "containers"}

#: Each workload's state is a promise the guide makes, so the containers are
#: pinned exactly, `checkout`'s script included: a `command`, `args`, or probe
#: added to `web` or `payments`, or a script that stops sleeping, would change
#: the state the reader is told to find. Like the reviewed README media, a
#: deliberate change updates the pin here.
CHECKOUT_SCRIPT = (
    'echo "checkout: connecting to the orders database"\n'
    'if [ -z "$DATABASE_URL" ]; then\n'
    '  echo "FATAL: DATABASE_URL is not set" >&2\n'
    "  exit 1\n"
    "fi\n"
    'echo "checkout: connected"\n'
    "while true; do sleep 3600; done\n"
)
EXPECTED_CONTAINERS = {
    "web": {"name": "web", "image": "nginx:1.27", "ports": [{"containerPort": 80}]},
    "checkout": {
        "name": "checkout",
        "image": "busybox:1.36",
        "command": ["sh", "-c", CHECKOUT_SCRIPT],
    },
    "payments": {"name": "payments", "image": "nginx:1.27-alpne"},
}
EXPECTED_REPLICAS = {"web": 2, "checkout": 1, "payments": 1}
EXPECTED_SERVICE_SPEC = {
    "selector": {"app": "web"},
    "ports": [{"name": "http", "port": 80, "targetPort": 80}],
}

#: Every document's metadata, pinned like the specs. An unreviewed label or
#: annotation changes what readers apply: a Pod Security `enforce: privileged`
#: label on the Namespace, say. The API server silently clears a `namespace`
#: on the cluster-scoped Namespace rather than rejecting it, so this pin, not
#: `kubectl apply`, is what catches one.
PART_OF = {"app.kubernetes.io/part-of": CLUSTER}
EXPECTED_METADATA: dict[tuple[str, str], dict[str, Any]] = {
    ("Namespace", NAMESPACE): {"name": NAMESPACE, "labels": PART_OF},
    ("Service", "web"): {
        "name": "web",
        "namespace": NAMESPACE,
        "labels": {"app": "web", **PART_OF},
    },
    **{
        ("Deployment", name): {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"app": name, **PART_OF},
        }
        for name in EXPECTED_REPLICAS
    },
}


def _documents() -> list[dict[str, Any]]:
    loaded = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    documents = [document for document in loaded if document is not None]
    assert all(isinstance(document, dict) for document in documents)
    return documents


def _deployments() -> dict[str, dict[str, Any]]:
    return {
        document["metadata"]["name"]: document
        for document in _documents()
        if document["kind"] == "Deployment"
    }


def _pod_spec(name: str) -> dict[str, Any]:
    spec = _deployments()[name]["spec"]["template"]["spec"]
    assert isinstance(spec, dict)
    return spec


def _checkout_script() -> str:
    command = _pod_spec("checkout")["containers"][0]["command"]
    assert command[:2] == ["sh", "-c"], "checkout must run one shell script"
    script = command[2]
    assert isinstance(script, str)
    return script


def _guide_section() -> str:
    guide = GUIDE.read_text(encoding="utf-8")
    # Exactly once: a second copy after the first would never be checked.
    assert guide.count(PRACTICE_HEADING) == 1, "the guide needs exactly one practice section"
    return guide.split(PRACTICE_HEADING, 1)[1].split("\n## ", 1)[0]


def _shell_blocks(section: str) -> list[list[str]]:
    blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
    return [[line.strip() for line in block.splitlines() if line.strip()] for block in blocks]


#: Inline code in the prose that a reader could paste into a shell. Pinned
#: exactly, like the command blocks, so any new command in the prose (an
#: unquoted `KUBECONFIG=...` prefix, a `kubectl` aimed elsewhere) is a
#: reviewed change. A span counts as a command when it has an argument or an
#: assignment and names a shell tool; single words like `kubectl` are names.
INLINE_COMMANDS = {
    f"kind delete cluster --name {CLUSTER}",
    f'$env:KUBECONFIG = "$PWD\\{CLUSTER}.kubeconfig"',
}
_SHELL_WORD = re.compile(
    r"\b(?:kubectl|kind|korvid|curl|rm|export|unset|helm|KUBECONFIG)\b|\$env:|--"
)
#: Every way the guide could assign KUBECONFIG: double-quoted, single-quoted,
#: or bare (`KUBECONFIG=$HOME/.kube/config kubectl ...`).
_KUBECONFIG_VALUE = re.compile(r"""KUBECONFIG\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s`'"]+))""")


def _inline_commands(section: str) -> set[str]:
    prose = re.sub(r"```sh\n.*?```", "", section, flags=re.DOTALL)
    spans = re.findall(r"`([^`\n]+)`", prose)
    return {span for span in spans if re.search(r"[\s=]", span) and _SHELL_WORD.search(span)}


def test_manifest_creates_only_the_playground_namespace_and_its_workloads() -> None:
    created = sorted(
        (document["kind"], document.get("apiVersion"), document["metadata"]["name"])
        for document in _documents()
    )

    # Pin the API version too: without it `kubectl apply` rejects the document.
    assert created == [
        ("Deployment", "apps/v1", "checkout"),
        ("Deployment", "apps/v1", "payments"),
        ("Deployment", "apps/v1", "web"),
        ("Namespace", "v1", NAMESPACE),
        ("Service", "v1", "web"),
    ]
    for document in _documents():
        key = (document["kind"], document["metadata"]["name"])
        assert document["metadata"] == EXPECTED_METADATA[key], key
        top_level = {"apiVersion", "kind", "metadata"}
        if document["kind"] != "Namespace":
            top_level.add("spec")
        assert set(document) == top_level, key


def test_workloads_set_only_reviewed_fields() -> None:
    for name, deployment in _deployments().items():
        spec = deployment["spec"]
        assert set(spec) == {"replicas", "selector", "template"}, name
        assert spec["replicas"] == EXPECTED_REPLICAS[name], name
        assert spec["selector"] == {"matchLabels": {"app": name}}, name
        template = spec["template"]
        assert set(template) == {"metadata", "spec"}, name
        assert template["metadata"] == {"labels": {"app": name, **PART_OF}}, name
        pod = _pod_spec(name)
        assert set(pod) == ALLOWED_POD_FIELDS, name
        assert pod["automountServiceAccountToken"] is False, name
        assert len(pod["containers"]) == 1, name


def test_each_workload_shows_the_state_the_guide_promises() -> None:
    containers = {name: _pod_spec(name)["containers"][0] for name in _deployments()}

    assert containers == EXPECTED_CONTAINERS, "each workload must keep its reviewed container"
    service = next(document for document in _documents() if document["kind"] == "Service")
    assert service["spec"] == EXPECTED_SERVICE_SPEC, "the Service stays a plain ClusterIP"
    assert service["spec"]["selector"] == _deployments()["web"]["spec"]["selector"]["matchLabels"]


@posix_only("the checkout script is a POSIX shell script")
def test_checkout_crashes_only_while_database_url_is_unset() -> None:
    """The manifest says checkout exits *because* DATABASE_URL is not set.

    A stub `sleep` returns twice and ends the run on its third call, so the
    configured run finishes instead of waiting forever, and only a script that
    keeps sleeping gets there: one that sleeps once and then exits is caught.
    """
    stub = 'n=0\nsleep() { n=$((n + 1)); [ "$n" -lt 3 ] || exit 7; }\n'
    script = stub + _checkout_script()
    path = {"PATH": os.environ.get("PATH", "")}

    unset = subprocess.run(
        ["sh", "-c", script], env=path, capture_output=True, text=True, timeout=30, check=False
    )
    configured = subprocess.run(
        ["sh", "-c", script],
        env={**path, "DATABASE_URL": "postgres://orders"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert unset.returncode == 1
    assert "FATAL: DATABASE_URL is not set" in unset.stderr
    assert configured.returncode == 7, "with DATABASE_URL set, checkout must keep running"
    assert "FATAL" not in configured.stderr


def test_the_guide_keeps_the_playground_in_its_own_kubeconfig() -> None:
    section = _guide_section()
    blocks = _shell_blocks(section)
    assert len(blocks) == 3, "the guide has a setup, an apply, and a cleanup block"
    setup, apply, cleanup = blocks

    # One chain: each step runs only if the one before it succeeded, so no
    # command can reach a real cluster or a reader's files after a failure.
    assert setup == [
        *SCRATCH_DIRECTORY,
        f"{DEDICATED_KUBECONFIG} &&",
        f"kind create cluster --name {CLUSTER} &&",
        f"curl -fsSLO {RAW_URL}",
    ], "enter a fresh directory and isolate the kubeconfig before any other command"
    # The apply block checks again that this terminal is in the scratch
    # directory with its own kubeconfig: a reader may carry on after a failed
    # setup, and korvid must never start on their usual clusters.
    assert apply == [
        f'cd "${{playground:?}}" && test "$KUBECONFIG" = "$PWD/{CLUSTER}.kubeconfig" &&',
        f"kubectl --context {CONTEXT} apply -f workloads.yaml &&",
        f"korvid -n {NAMESPACE}",
    ]
    assert RAW_URL.endswith(MANIFEST.relative_to(ROOT).as_posix())
    fences = [match.start() for match in re.finditer(r"```sh\n", section)]
    review = section.index("Read `workloads.yaml` before you apply it")
    assert fences[0] < review < fences[1], "ask for the review between download and apply"
    kubectl = [line for block in blocks for line in block if line.startswith("kubectl")]
    assert kubectl
    assert all(f"--context {CONTEXT}" in line for line in kubectl)
    assert cleanup == [
        f"kind delete cluster --name {CLUSTER} &&",
        'cd && rm -r "${playground:?}"',
    ], "the cleanup deletes the guide's own directory only once the cluster is gone"
    assert "open a new terminal" in section.lower(), (
        "only a new terminal restores a KUBECONFIG the reader had set before"
    )
    for name in ("web", "checkout", "payments"):
        assert f"`{name}`" in section, f"the guide must say what to look for in {name}"


def test_every_command_in_the_guide_targets_only_the_playground() -> None:
    """Inline commands are commands too: the re-run tip and the PowerShell line
    are run by readers, so like the code blocks they are pinned, and they may
    name only the throwaway cluster, its context, and its own kubeconfig file."""
    section = _guide_section()
    fences = re.findall(r"^[ \t]*(?:`{3,}|~{3,}).*$", section, flags=re.MULTILINE)
    names = re.findall(r"""--name[ =]["']?([^\s"'`]+)""", section)
    contexts = re.findall(r"""--context[ =]["']?([^\s"'`]+)""", section)
    kubeconfigs = ["".join(value) for value in _KUBECONFIG_VALUE.findall(section)]

    assert fences == ["```sh", "```"] * 3, "only the three pinned sh blocks may hold commands"
    assert _inline_commands(section) == INLINE_COMMANDS
    assert names, "the guide must name its kind cluster"
    assert set(names) == {CLUSTER}, names
    assert contexts, "the guide must pin kubectl to the playground context"
    assert set(contexts) == {CONTEXT}, contexts
    assert kubeconfigs, "the guide must set a dedicated KUBECONFIG"
    for path in kubeconfigs:
        assert re.fullmatch(rf"\$PWD[/\\]{CLUSTER}\.kubeconfig", path), path
    for flag in RETARGETING:
        assert flag not in section, f"{flag} would point kubectl away from the playground"


def test_the_practice_section_is_the_reviewed_text() -> None:
    digest = hashlib.sha256(_guide_section().encode("utf-8")).hexdigest()

    assert digest == REVIEWED_SECTION_SHA256, (
        "the practice section changed: re-check that every command still reaches only "
        "the throwaway cluster, then update REVIEWED_SECTION_SHA256"
    )
