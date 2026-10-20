#!/usr/bin/env python3
"""Labeled Recall@3 through authenticated runtime API; no real chat/model diagnosis."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.aiops_client import query


def evaluate(output):
    fixtures = json.loads(Path(__file__).with_name("rag_eval_queries.json").read_text())
    documents = {x["document_id"]: x for x in query("documents")["items"]}
    rows = []
    correct = 0
    citations = 0
    valid = 0
    for fixture in fixtures:
        response = query("search", search_query=fixture["query"])
        assert response["retrieval_method"] == "pgvector-cosine"
        assert response["context"]["trust"] == "untrusted_reference_data"
        items = response["items"]
        hit = any(x["source_path"].endswith("/" + fixture["expected"]) for x in items)
        correct += hit
        for item in items:
            citations += 1
            doc = documents.get(item["document_id"])
            if (
                doc
                and item["version"] == doc["version"]
                and item["source_path"] == doc["source_path"]
                and hashlib.sha256(item["content"].encode()).hexdigest() == item["checksum"]
            ):
                valid += 1
        rows.append(
            {
                **fixture,
                "hit": hit,
                "citations": [
                    {
                        k: x[k]
                        for k in [
                            "document_id",
                            "chunk_id",
                            "source_path",
                            "version",
                            "checksum",
                            "score",
                            "model_id",
                            "dimension",
                        ]
                    }
                    for x in items
                ],
            }
        )
    report = {
        "workflow": "hypothesis-rag-v2",
        "retrieval_method": "pgvector-cosine",
        "queries": len(rows),
        "hits": correct,
        "recall_at_3": correct / len(rows),
        "citations": citations,
        "valid_citations": valid,
        "citation_correctness": valid / citations if citations else 0,
        "documents": list(documents.values()),
        "results": rows,
    }
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "queries",
                    "hits",
                    "recall_at_3",
                    "citations",
                    "valid_citations",
                    "citation_correctness",
                ]
            }
        )
    )
    if correct / len(rows) < 0.8 or valid != citations:
        raise SystemExit("Retrieval acceptance failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/aiops-rag-evaluation.json")
    evaluate(parser.parse_args().output)
