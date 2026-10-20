#!/usr/bin/env python3
"""Install video log collection into an existing Prometheus/Grafana namespace."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_common import command, validate, deployment_lock


def setup():
    with deployment_lock():
        validate()
        command("kubectl", "get", "namespace", "monitoring", "-o", "name", capture=True)
        command("kubectl", "apply", "-f", "k8s/log-collector-rbac.yaml", capture=True)
        command(
            "helm", "repo", "add", "grafana", "https://grafana.github.io/helm-charts", capture=True
        )
        # Keep the existing Loki release/service name and PVC identity.
        command(
            "helm",
            "upgrade",
            "--install",
            "p1-loki",
            "grafana/loki",
            "--version",
            "7.3.0",
            "-n",
            "monitoring",
            "-f",
            "k8s/loki-values.yaml",
            "--wait",
            "--timeout",
            "5m",
        )
        command(
            "helm",
            "upgrade",
            "--install",
            "p1-alloy",
            "grafana/alloy",
            "--version",
            "1.13.1",
            "-n",
            "monitoring",
            "-f",
            "k8s/alloy-values.yaml",
            "--wait",
            "--timeout",
            "3m",
        )
        command("kubectl", "apply", "-f", "k8s/grafana-loki-datasource.yaml", capture=True)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    setup()
