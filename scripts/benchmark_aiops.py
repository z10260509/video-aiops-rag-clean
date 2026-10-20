#!/usr/bin/env python3
"""Bounded original-video diagnosis repetitions; preserve failures and never approve."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_video_scenario import run


def benchmark(source, repeats, output):
    output = Path(output)
    if not 1 <= repeats <= 5:
        raise ValueError("Bounded repeats 1..5")
    if output.exists():
        raise ValueError("Output exists; previous results are never overwritten")
    folder = output.parent / (output.stem + "-runs")
    if folder.exists():
        raise ValueError("Run folder exists; choose a fresh output path")
    folder.mkdir(parents=True)
    result = {
        "workflow": "hypothesis-rag-v2",
        "scenario": "original-video-ffmpeg-invalid-codec",
        "runs": [],
        "operator_approval_submitted": False,
        "repair_executed": False,
    }
    try:
        for index in range(repeats):
            path = folder / (str(index + 1).zfill(2) + ".json")
            try:
                data = run(source, path, fault=True)
            except Exception:
                data = json.loads(path.read_text()) if path.exists() else {"status": "failed"}
                result["runs"].append({"artifact": str(path), "result": data})
                raise
            result["runs"].append({"artifact": str(path), "result": data})
        result["status"] = "passed"
        return result
    except Exception:
        result["status"] = "failed"
        raise
    finally:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    benchmark(args.source, args.repeat, args.output)
