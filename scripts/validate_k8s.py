#!/usr/bin/env python3
"""Validate rendered current-system contracts, without applying any cluster resources."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_common import render_all
from scripts.project_images import CONTROL, VIDEO, UI


def validate_documents(documents):
    def item(kind, namespace, name):
        return next(
            d
            for d in documents
            if d["kind"] == kind
            and d["metadata"].get("namespace") == namespace
            and d["metadata"]["name"] == name
        )

    for ns, name, image in [
        ("video-platform", "backend", CONTROL),
        ("video-platform", "worker", VIDEO),
        ("video-platform", "frontend", UI),
        ("video-platform", "worker-metrics", VIDEO),
        ("aiops", "aiops-api", CONTROL),
        ("aiops", "aiops-graph-worker", CONTROL),
        ("aiops", "aiops-executor", CONTROL),
    ]:
        obj = item("Deployment", ns, name)
        container = obj["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == image, (ns, name, "image mismatch")
        if name in {"backend", "worker"}:
            env = {e["name"]: e for e in container["env"]}
            assert env["AIOPS_VIDEO_ENABLED"]["value"] == "true"
            assert {"POD_NAME", "POD_UID", "KUBERNETES_NAMESPACE"} <= set(env)
            assert obj["metadata"]["labels"]["aiops.video-platform/repair-enabled"] == "true"
            assert (
                obj["spec"]["template"]["metadata"]["labels"]["aiops.video-platform/repair-enabled"]
                == "true"
            )
            if name == "backend":
                assert {
                    "AIOPS_VIDEO_READER_TOKEN",
                    "AIOPS_VIDEO_REPAIR_TOKEN",
                    "AIOPS_OPERATOR_TOKEN",
                } <= set(env)
    video = item("Job", "video-platform", "video-schema-v2")["spec"]["template"]["spec"][
        "containers"
    ][0]
    control = item("Job", "aiops", "aiops-schema-v2")["spec"]["template"]["spec"]["containers"][0]
    assert (
        "provision_video_schema" in video["command"][-1] and "alembic" not in video["command"][-1]
    )
    assert "alembic upgrade head" in control["command"][-1]
    assert video["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "video-platform-secrets"
    assert control["env"][0]["valueFrom"]["secretKeyRef"]["key"] == "OWNER_DATABASE_URL"
    assert not any(
        d["kind"] == "Deployment" and d["metadata"]["name"] == "aiops-dashboard" for d in documents
    )
    assert not any(d["metadata"].get("name") == "pod-observer" for d in documents)
    ingress = item("Ingress", "video-platform", "video-platform")
    assert any(
        p["path"] == "/" and p["backend"]["service"]["name"] == "frontend"
        for p in ingress["spec"]["rules"][0]["http"]["paths"]
    )
    assert item("PodDisruptionBudget", "video-platform", "frontend")["spec"]["minAvailable"] == 1
    assert item("Deployment", "aiops", "aiops-executor")["spec"]["replicas"] == 0
    assert item("ConfigMap", "aiops", "aiops-config")["data"]["REMEDIATION_ENABLED"] == "false"
    for doc in documents:
        assert doc.get("metadata", {}).get("namespace") != "chaos-lab"
    return {
        "resources": len(documents),
        "integrated_frontend": True,
        "exporter_included": True,
        "video_control_schema_separated": True,
        "manual_approval_retained": True,
        "applied_to_cluster": False,
    }


if __name__ == "__main__":
    print(json.dumps(validate_documents(render_all())))
