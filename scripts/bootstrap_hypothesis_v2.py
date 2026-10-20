#!/usr/bin/env python3
"""Initialize current AIOps on an existing video platform and monitoring installation.

Refuses an existing AIOps API: use setup_hypothesis_v2 for upgrades. Never resets storage.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_common import (
    command,
    apply,
    render,
    validate,
    update,
    secret_env,
    deployment_lock,
)
from scripts.project_images import CONTROL, VIDEO, UI, DATABASE
from scripts.image_versions import build

CONTROL_SCHEMA = "cd /app/backend && alembic upgrade head && python -m app.aiops.checkpoints && python -m app.aiops.provision && python -m app.aiops.provision_executor"


def ensure_secret(namespace, name, values):
    raw = command(
        "kubectl",
        "-n",
        namespace,
        "get",
        "secret",
        name,
        "--ignore-not-found",
        "-o",
        "json",
        capture=True,
    )
    if raw.strip():
        data = json.loads(raw)["data"]
        if not set(values) <= set(data):
            raise ValueError("Existing secret lacks required keys: " + name)
        return {key: base64.b64decode(data[key]).decode() for key in values}
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "stringData": values,
        }
    )
    return values


def schema_job(namespace, image, program, env):
    name = "hypothesis-schema-" + uuid.uuid4().hex[:12]
    apply(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 180,
                "template": {
                    "spec": {
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "schema",
                                "image": image,
                                "command": ["sh", "-c", program],
                                "env": env,
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "128Mi"},
                                    "limits": {"cpu": "500m", "memory": "512Mi"},
                                },
                            }
                        ],
                    }
                },
            },
        }
    )
    # Keep failed Job/logs for inspection, never retry a failed migration automatically.
    command(
        "kubectl",
        "-n",
        namespace,
        "wait",
        "--for=condition=complete",
        "job/" + name,
        "--timeout=180s",
    )
    command("kubectl", "-n", namespace, "delete", "job", name, capture=True)


def bootstrap(ollama_url, build_images=False, dry_run=False):
    with deployment_lock():
        validate()
        existing = command(
            "kubectl",
            "-n",
            "aiops",
            "get",
            "deployment",
            "aiops-api",
            "--ignore-not-found",
            "-o",
            "name",
            capture=True,
        )
        if existing.strip():
            raise ValueError("AIOps API already exists; use setup_hypothesis_v2.py for upgrades")
        from urllib.parse import urlsplit

        parsed = urlsplit(ollama_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Explicit reachable Ollama HTTP URL required; no credentials or query")
        if dry_run:
            print(
                json.dumps(
                    {
                        "mode": "bootstrap-hypothesis-v2",
                        "control": CONTROL,
                        "worker": VIDEO,
                        "ui": UI,
                        "database": DATABASE,
                        "migrations": "head + official checkpoints",
                        "repairs_enabled": False,
                        "storage_deleted": False,
                    }
                )
            )
            return
        # Read-only preflight before any cluster writes, including queued jobs.
        code = "from app.database.session import engine;from sqlalchemy import text;c=engine.connect();assert c.execute(text(\"SELECT count(*) FROM jobs WHERE status IN ('PROCESSING','QUEUED')\")).scalar()==0,'Wait for video jobs'"
        command(
            "kubectl",
            "-n",
            "video-platform",
            "exec",
            "deployment/backend",
            "--",
            "python",
            "-c",
            code,
            capture=True,
        )
        check = "import httpx;r=httpx.get(BASE+'/api/tags',timeout=10);r.raise_for_status();names={x['name'] for x in r.json()['models']};assert {'gemma4:e2b-it-qat','embeddinggemma:300m'}<=names".replace(
            "BASE", repr(ollama_url.rstrip("/"))
        )
        command(
            "kubectl",
            "-n",
            "video-platform",
            "exec",
            "deployment/backend",
            "--",
            "python",
            "-c",
            check,
            capture=True,
        )
        if build_images:
            for image, dockerfile in [
                (CONTROL, "backend/Dockerfile"),
                (VIDEO, "worker/Dockerfile"),
            ]:
                build(image, dockerfile)
            for image, dockerfile in [
                (UI, "frontend/Dockerfile.aiops"),
                (DATABASE, "backend/Dockerfile.pgvector"),
            ]:
                if subprocess.run(
                    ["docker", "image", "inspect", image], capture_output=True
                ).returncode:
                    build(image, dockerfile)
        command(
            "kind", "load", "docker-image", CONTROL, VIDEO, UI, DATABASE, "--name", "video-platform"
        )
        with tempfile.NamedTemporaryFile(
            prefix="video-before-hypothesis-", suffix=".dump", delete=False
        ) as backup:
            os.chmod(backup.name, 0o600)
            result = subprocess.run(
                [
                    "kubectl",
                    "-n",
                    "video-platform",
                    "exec",
                    "statefulset/postgres",
                    "--",
                    "sh",
                    "-c",
                    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                ],
                stdout=backup,
                stderr=subprocess.PIPE,
            )
            if result.returncode:
                raise RuntimeError("Video database backup failed")
            print("Private original video DB backup: " + backup.name)
        command("kubectl", "apply", "-f", "k8s/aiops/namespace.yaml", capture=True)
        owner, runtime = secrets.token_hex(24), secrets.token_hex(24)
        ensure_secret(
            "aiops",
            "aiops-secrets",
            {
                "POSTGRES_PASSWORD": owner,
                "AIOPS_RUNTIME_PASSWORD": runtime,
                "OWNER_DATABASE_URL": f"postgresql+psycopg://aiops_owner:{owner}@aiops-postgres:5432/aiops_control",
                "DATABASE_URL": f"postgresql+psycopg://aiops_app:{runtime}@aiops-postgres:5432/aiops_control",
                "webhook-token": secrets.token_hex(32),
                "operator-token": secrets.token_hex(32),
            },
        )
        executor = secrets.token_hex(24)
        executor_values = ensure_secret(
            "aiops",
            "aiops-executor",
            {
                "password": executor,
                "DATABASE_URL": f"postgresql+psycopg://aiops_executor:{executor}@aiops-postgres:5432/aiops_control",
            },
        )
        for obj in render("k8s/aiops/postgres.yaml"):
            apply(obj)
        command(
            "kubectl",
            "-n",
            "aiops",
            "rollout",
            "status",
            "deployment/aiops-postgres",
            "--timeout=180s",
        )
        schema_job(
            "aiops",
            CONTROL,
            CONTROL_SCHEMA + " && python -m app.aiops.knowledge",
            [
                secret_env("DATABASE_URL", "aiops-secrets", "OWNER_DATABASE_URL"),
                secret_env("AIOPS_RUNTIME_PASSWORD", "aiops-secrets", "AIOPS_RUNTIME_PASSWORD"),
                secret_env("AIOPS_EXECUTOR_PASSWORD", "aiops-executor", "password"),
                {"name": "KNOWLEDGE_VECTORS_ENABLED", "value": "true"},
                {"name": "KNOWLEDGE_EMBEDDING_PROVIDER", "value": "ollama"},
                {"name": "OLLAMA_BASE_URL", "value": ollama_url},
            ],
        )
        raw = json.loads(
            command(
                "kubectl",
                "-n",
                "aiops",
                "get",
                "secret",
                "aiops-secrets",
                "-o",
                "json",
                capture=True,
            )
        )
        operator = base64.b64decode(raw["data"]["operator-token"]).decode()
        video = ensure_secret(
            "video-platform",
            "aiops-video-integration",
            {
                "reader-token": secrets.token_hex(32),
                "repair-token": secrets.token_hex(32),
                "operator-token": operator,
            },
        )
        if video["operator-token"] != operator:
            raise ValueError(
                "Existing video operator identity differs; manual reconciliation required"
            )
        source = ensure_secret(
            "aiops", "aiops-video-source", {"reader-token": video["reader-token"]}
        )
        if source["reader-token"] != video["reader-token"]:
            raise ValueError("Existing video reader identities differ")
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "aiops-executor", "namespace": "aiops"},
                "stringData": {
                    **executor_values,
                    "VIDEO_READER_TOKEN": video["reader-token"],
                    "VIDEO_REPAIR_TOKEN": video["repair-token"],
                },
            }
        )
        schema_job(
            "video-platform",
            CONTROL,
            "python -m app.services.provision_video_schema",
            [secret_env("DATABASE_URL", "video-platform-secrets", "DATABASE_URL")],
        )
        for path in [
            "reader-rbac.yaml",
            "video-integration-rbac.yaml",
            "api.yaml",
            "graph-worker.yaml",
            "executor.yaml",
            "metrics.yaml",
            "grafana-dashboard.yaml",
        ]:
            for obj in render("k8s/aiops/" + path):
                if obj["kind"] == "ConfigMap":
                    obj["data"].update(
                        OLLAMA_BASE_URL=ollama_url,
                        AIOPS_VIDEO_SCAN_SINCE=datetime.now(timezone.utc).isoformat(),
                        REMEDIATION_ENABLED="false",
                        AIOPS_HYPOTHESIS_REMEDIATION_ENABLED="false",
                    )
                apply(obj)
        enabled = [
            {"name": "AIOPS_VIDEO_ENABLED", "value": "true"},
            {"name": "AIOPS_VIDEO_DEMO_ENABLED", "value": "true"},
        ]
        podenv = [
            {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
            {"name": "KUBERNETES_NAMESPACE", "value": "video-platform"},
        ]
        update(
            "video-platform",
            "backend",
            CONTROL,
            enabled
            + podenv
            + [
                secret_env("AIOPS_VIDEO_READER_TOKEN", "aiops-video-integration", "reader-token"),
                secret_env("AIOPS_VIDEO_REPAIR_TOKEN", "aiops-video-integration", "repair-token"),
                secret_env("AIOPS_OPERATOR_TOKEN", "aiops-video-integration", "operator-token"),
            ],
            True,
        )
        update("video-platform", "worker", VIDEO, enabled + podenv, True)
        for path in [
            "k8s/frontend.yaml",
            "k8s/worker-metrics.yaml",
            "k8s/ingress.yaml",
            "k8s/pdb.yaml",
            "k8s/servicemonitor.yaml",
            "k8s/prometheus-rule.yaml",
        ]:
            for obj in render(path):
                apply(obj)
        for ns, name in [
            ("video-platform", "backend"),
            ("video-platform", "worker"),
            ("aiops", "aiops-api"),
            ("aiops", "aiops-graph-worker"),
            ("video-platform", "frontend"),
            ("video-platform", "worker-metrics"),
        ]:
            command(
                "kubectl", "-n", ns, "rollout", "status", "deployment/" + name, "--timeout=180s"
            )
        print(
            "Current v2 initialized; repairs disabled. Use setup_hypothesis_v2.py --enable-remediation after review."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama-url", required=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    bootstrap(args.ollama_url, args.build, args.dry_run)
