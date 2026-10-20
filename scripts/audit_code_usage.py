#!/usr/bin/env python3
"""Conservative static audit; dynamic dispatch and historical workflows need review."""

import argparse
import ast
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def audit():
    modules = {}
    trees = {}
    references = Counter()
    errors = []
    for base, prefix in [
        ("backend/app", "app"),
        ("worker/app", "worker.app"),
        ("scripts", "scripts"),
    ]:
        for path in sorted((ROOT / base).rglob("*.py")):
            relative = path.relative_to(ROOT / base).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            name = ".".join([prefix, *parts])
            modules[name] = str(path.relative_to(ROOT))
            try:
                trees[name] = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError as exc:
                errors.append(
                    {"path": str(path.relative_to(ROOT)), "line": exc.lineno, "error": exc.msg}
                )
    edges = {}
    missing = []
    candidates = []
    for module, tree in trees.items():
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                references[node.id] += 1
            if isinstance(node, ast.Attribute):
                references[node.attr] += 1
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                base = node.module or ""
                targets = [base] + [
                    base + "." + alias.name
                    for alias in node.names
                    if base + "." + alias.name in modules
                ]
            for target in targets:
                if target in modules:
                    imports.add(target)
                elif target.startswith(("app.", "worker.app.", "scripts.")):
                    # Namespace packages without __init__.py are valid too.
                    package_path = (
                        ROOT
                        / ("backend/" if target.startswith("app.") else "")
                        / target.replace(".", "/")
                    )
                    if not package_path.is_dir():
                        missing.append(
                            {"path": modules[module], "line": node.lineno, "module": target}
                        )
        edges[module] = sorted(imports)
    # Production entrypoints plus modules dynamically registered by Celery.
    roots = [
        "app.main",
        "app.aiops.main",
        "app.aiops.graph_worker",
        "app.aiops.executor",
        "app.aiops.provision",
        "app.aiops.provision_executor",
        "app.aiops.checkpoints",
        "app.aiops.knowledge",
        "app.services.provision_video_schema",
        "app.observability.exporter",
        "worker.app.celery_app",
        "worker.app.tasks",
    ]
    reachable = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in reachable or current not in modules:
            continue
        reachable.add(current)
        pending.extend(edges.get(current, []))
        parts = current.split(".")
        pending.extend(
            ".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in modules
        )
    for module, tree in trees.items():
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.decorator_list
                and references[node.name] == 0
            ):
                candidates.append(
                    {
                        "path": modules[module],
                        "line": node.lineno,
                        "symbol": node.name,
                        "classification": "No static name/attribute references in application or scripts; not proof of dead code",
                    }
                )
    return {
        "method": "AST imports and name/attribute references; no runtime tracing; tests are not runtime roots",
        "limitations": [
            "Decorators, callbacks, reflection, command strings, Celery registration and deployment bootstrap need manual review.",
            "A module reachable through an import is not proof every function executes.",
            "Historical checkpoint resumption and data migrations are compatibility requirements.",
        ],
        "runtime_roots": roots,
        "missing_local_imports": missing,
        "parse_errors": errors,
        "modules_outside_static_runtime_graph": [
            {"module": m, "path": modules[m]} for m in sorted(set(modules) - reachable)
        ],
        "zero_reference_top_level_function_candidates": candidates,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/aiops-code-usage-scan.json"))
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "parse_errors": len(result["parse_errors"]),
                "missing_local_imports": len(result["missing_local_imports"]),
                "candidate_functions": len(result["zero_reference_top_level_function_candidates"]),
            }
        )
    )
    if result["parse_errors"] or result["missing_local_imports"]:
        raise SystemExit(1)
