#!/usr/bin/env python3
"""Opt-in local v2 rollout; preserve storage, credentials, schema and read-only history."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_common import command, validate, update, deployment_lock, render_all, apply
from scripts.image_versions import build

from scripts.project_images import CONTROL, VIDEO, UI

TARGETS = [
    ("video-platform", "backend", CONTROL),
    ("video-platform", "worker", VIDEO),
    ("aiops", "aiops-api", CONTROL),
    ("aiops", "aiops-graph-worker", CONTROL),
    ("aiops", "aiops-executor", CONTROL),
    ("video-platform", "frontend", UI),
    ("video-platform", "worker-metrics", VIDEO),
]


def setup(build_images=False, enable_remediation=False, dry_run=False):
    with deployment_lock():
        return rollout(build_images, enable_remediation, dry_run)


def rollout(build_images=False, enable_remediation=False, dry_run=False):
    validate()
    if dry_run:
        print(
            json.dumps(
                {
                    "targets": TARGETS,
                    "mode": "hypothesis-v2",
                    "v2_repairs": enable_remediation,
                    "schema_migration": False,
                    "volumes_replaced": False,
                },
                indent=2,
            )
        )
        return
    if build_images:
        for image, file in [(CONTROL, "backend/Dockerfile"), (VIDEO, "worker/Dockerfile")]:
            build(image, file)
        import subprocess

        if subprocess.run(["docker", "image", "inspect", UI], capture_output=True).returncode:
            build(UI, "frontend/Dockerfile.aiops")
    # Check readiness and in-flight work before any cluster mutation. No automatic cancellations.
    original = "from app.database.session import engine;from sqlalchemy import text;c=engine.connect();assert c.execute(text(\"SELECT count(*) FROM jobs WHERE status IN ('PROCESSING','QUEUED')\")).scalar()==0,'Wait for original video jobs to finish'"
    control = "from app.database.session import engine;from sqlalchemy import text;from app.core.config import get_settings;assert get_settings().aiops_video_enabled;c=engine.connect();assert c.execute(text(\"SELECT count(*) FROM diagnosis_runs WHERE status IN ('pending','running','waiting')\")).scalar()==0,'Finish or reject pending diagnoses/approvals first';assert c.execute(text(\"SELECT count(*) FROM aiops_actions WHERE execution_status IN ('queued','running')\")).scalar()==0,'Wait for repairs to finish'"
    command(
        "kubectl",
        "-n",
        "video-platform",
        "exec",
        "deployment/backend",
        "--",
        "python",
        "-c",
        original,
        capture=True,
    )
    command(
        "kubectl",
        "-n",
        "aiops",
        "exec",
        "deployment/aiops-api",
        "--",
        "python",
        "-c",
        control,
        capture=True,
    )
    for ns, name, _ in TARGETS:
        if name != "worker-metrics":
            command("kubectl", "-n", ns, "get", "deployment", name, "-o", "name", capture=True)
    documents = render_all()
    # Existing installations already have the scoped executor role. Grant only the
    # new policy reads before any v2 worker can consume an approved action.
    command(
        "kubectl",
        "-n",
        "aiops",
        "exec",
        "deployment/aiops-postgres",
        "--",
        "sh",
        "-c",
        'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        '-c "GRANT SELECT ON diagnosis_runs, aiops_timeline TO aiops_executor"',
        capture=True,
    )
    command("kind", "load", "docker-image", CONTROL, VIDEO, UI, "--name", "video-platform")
    command(
        "kubectl",
        "-n",
        "aiops",
        "patch",
        "configmap",
        "aiops-config",
        "--type",
        "merge",
        "-p",
        json.dumps(
            {
                "data": {
                    "AIOPS_HYPOTHESIS_REMEDIATION_ENABLED": str(enable_remediation).lower(),
                    "REMEDIATION_ENABLED": str(enable_remediation).lower(),
                    "AIOPS_APPROVAL_ENABLED": "true",
                }
            }
        ),
        capture=True,
    )
    manifests = {
        (d.get("metadata", {}).get("namespace"), d.get("metadata", {}).get("name")): d
        for d in documents
        if d["kind"] == "Deployment"
    }
    for ns, name, image in TARGETS:
        desired = manifests[(ns, name)]
        if name == "worker-metrics":
            apply(desired)
        else:
            env = desired["spec"]["template"]["spec"]["containers"][0].get("env", [])
            update(ns, name, image, env, labels=name in {"backend", "worker"})
        command("kubectl", "-n", ns, "rollout", "status", "deployment/" + name, "--timeout=180s")
    # Only application access/monitoring resources; never reapply PVCs, DBs or schema Jobs on upgrade.
    for d in documents:
        if d["kind"] in {
            "Service",
            "ServiceMonitor",
            "PrometheusRule",
            "Ingress",
            "PodDisruptionBudget",
            "Role",
            "RoleBinding",
            "ServiceAccount",
        }:
            if d["kind"] == "Service" and d["metadata"]["name"] in {
                "postgres",
                "redis",
                "aiops-postgres",
            }:
                continue
            apply(d)
        if (
            d["kind"] == "ConfigMap"
            and d["metadata"].get("labels", {}).get("grafana_dashboard") == "1"
        ):
            apply(d)
    command(
        "kubectl",
        "-n",
        "aiops",
        "scale",
        "deployment/aiops-executor",
        "--replicas=" + str(int(enable_remediation)),
        capture=True,
    )
    command(
        "kubectl", "-n", "aiops", "rollout", "status", "deployment/aiops-executor", "--timeout=180s"
    )
    # Same repository source IDs as bootstrap; runtime DML only, no schema/index changes.
    # Immutable historical chunks remain available to existing citations.
    code = """from pathlib import Path
import json
from app.database.session import SessionLocal
from app.aiops.knowledge import import_repository
with SessionLocal() as db:
    indexed=import_repository(db,Path('/app/runbooks'))
    db.commit()
    print(json.dumps({'indexed':indexed}))"""
    command(
        "kubectl",
        "-n",
        "aiops",
        "exec",
        "deployment/aiops-api",
        "-i",
        "--",
        "python",
        "-",
        input_text=code,
        capture=True,
    )
    print(
        "Hypothesis v2 installed; v2 repairs "
        + (
            "require all existing policies and human approval"
            if enable_remediation
            else "disabled (read-only investigation)"
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--enable-remediation", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    setup(args.build, args.enable_remediation, args.dry_run)
