"""Local immutable tags: check Docker and every target Kind node before building."""

import subprocess
import json


def build(image, dockerfile):
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        raise ValueError("Image tag already exists; use a new version tag")
    nodes = subprocess.check_output(
        [
            "docker",
            "ps",
            "--filter",
            "label=io.x-k8s.kind.cluster=video-platform",
            "--format",
            "{{.Names}}",
        ],
        text=True,
    ).split()
    for node in nodes:
        obj = json.loads(
            subprocess.check_output(
                ["docker", "exec", node, "crictl", "images", "-o", "json"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        )
        if any(
            tag == image or tag.endswith("/" + image)
            for row in obj.get("images", [])
            for tag in row.get("repoTags", [])
        ):
            raise ValueError("Image tag already exists in Kind; use a new version tag")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"]))
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            image,
            "--label",
            "org.opencontainers.image.revision=" + revision,
            "--label",
            "aiops.source-dirty=" + str(dirty).lower(),
            "-f",
            dockerfile,
            ".",
        ],
        check=True,
    )
