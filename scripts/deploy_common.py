"""Shared local deployment helpers; no historical installers or implicit mutations."""

import fcntl
import json
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args, input_text=None, capture=False):
    result = subprocess.run(args, cwd=ROOT, input=input_text, text=True, capture_output=capture)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(args[:3]))
    return result.stdout


def apply(document):
    command("kubectl", "apply", "-f", "-", input_text=json.dumps(document), capture=True)


def decode_objects(raw):
    decoder = json.JSONDecoder()
    resources = []
    while raw.strip():
        obj, end = decoder.raw_decode(raw.lstrip())
        resources.extend(obj.get("items", [obj]))
        raw = raw.lstrip()[end:]
    return resources


def render_all():
    rendered = command("kubectl", "kustomize", "k8s", capture=True)
    raw = command(
        "kubectl",
        "create",
        "--dry-run=client",
        "--validate=false",
        "-f",
        "-",
        "-o",
        "json",
        input_text=rendered,
        capture=True,
    )
    return decode_objects(raw)


def render(path):
    raw = command(
        "kubectl",
        "create",
        "--dry-run=client",
        "--validate=false",
        "-f",
        path,
        "-o",
        "json",
        capture=True,
    )
    wanted = decode_objects(raw)

    def identity(obj):
        return (
            obj["kind"],
            obj.get("metadata", {}).get("namespace", ""),
            obj.get("metadata", {}).get("name"),
        )

    resolved = {identity(obj): obj for obj in render_all()}
    return [resolved[identity(obj)] for obj in wanted]


def validate():
    if (
        command("kubectl", "config", "current-context", capture=True).strip()
        != "kind-video-platform"
    ):
        raise ValueError("Expected kind-video-platform context")
    command("kubectl", "get", "namespace", "video-platform", "-o", "name", capture=True)
    raw = command(
        "kubectl", "get", "namespace", "aiops", "--ignore-not-found", "-o", "json", capture=True
    )
    if (
        raw.strip()
        and json.loads(raw)["metadata"].get("labels", {}).get("aiops.video-platform/managed")
        != "true"
    ):
        raise ValueError("AIOps namespace is not owned by this installation")


def secret_env(name, secret, key):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def update(namespace, name, image, env, labels=False):
    obj = json.loads(
        command("kubectl", "-n", namespace, "get", "deployment", name, "-o", "json", capture=True)
    )
    template = obj["spec"]["template"]
    container = template["spec"]["containers"][0]
    container["image"] = image
    replaced = {e["name"] for e in env}
    container["env"] = [e for e in container.get("env", []) if e["name"] not in replaced] + env
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": obj["metadata"]["resourceVersion"],
        },
    ]
    if labels:
        template["metadata"].setdefault("labels", {})["aiops.video-platform/repair-enabled"] = (
            "true"
        )
        metadata = dict(obj["metadata"].get("labels", {}))
        metadata["aiops.video-platform/repair-enabled"] = "true"
        operations.append({"op": "add", "path": "/metadata/labels", "value": metadata})
    operations.append({"op": "replace", "path": "/spec/template", "value": template})
    command(
        "kubectl",
        "-n",
        namespace,
        "patch",
        "deployment",
        name,
        "--type=json",
        "-p",
        json.dumps(operations),
        capture=True,
    )


@contextmanager
def deployment_lock():
    with (Path(tempfile.gettempdir()) / "video-platform-aiops-deployment.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another deployment is running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
