import json
from copy import deepcopy
import httpx
import pytest
from app.aiops.observation_fields import normalize_logs, field_catalog
from app.aiops.hypothesis_model import HypothesisModel
from app.aiops.hypotheses import evaluate
from app.aiops.hypothesis_validation import parse_round
from app.core.config import Settings


def observations():
    raw = {
        "lines": [
            {
                "line": json.dumps(
                    {
                        "namespace": "video-platform",
                        "pod": "worker-test",
                        "job_id": "j",
                        "task_id": "t",
                        "error": "Unknown encoder bad",
                    }
                )
            }
        ]
    }
    logs = normalize_logs(raw, "video-platform", "worker-test", {"job_id": "j", "task_id": "t"})
    return [
        {
            "evidence_id": "task",
            "kind": "get_task_status",
            "source": "video_task_api",
            "errors": [],
            "data": {"error": "Unknown encoder bad", "execution_status": "failed", "oom_kills": 0},
        },
        {
            "evidence_id": "encoders",
            "kind": "get_encoder_inventory",
            "source": "video_task_api",
            "errors": [],
            "data": {"known_encoders": {"bad": False, "libx264": True}},
        },
        {"evidence_id": "logs", "kind": "query_logs", "source": "loki", "errors": [], "data": logs},
    ]


def test_normalized_logs_preserve_raw_and_never_parse_other_tasks_or_pods():
    def line(**change):
        return {
            "line": json.dumps(
                {
                    "namespace": "video-platform",
                    "pod": "worker-test",
                    "job_id": "j",
                    "task_id": "t",
                    "error": "actual",
                    **change,
                }
            )
        }

    raw = {
        "lines": [
            line(),
            line(task_id="other"),
            line(pod="other"),
            line(namespace="other"),
            {"line": "not JSON"},
        ],
        "parsed_lines": [{"error": "injected"}],
    }
    original = deepcopy(raw)
    normalized = normalize_logs(
        raw, "video-platform", "worker-test", {"job_id": "j", "task_id": "t"}
    )
    assert raw == original and normalized["lines"] == raw["lines"]
    assert (
        len(normalized["parsed_lines"]) == 1 and normalized["parsed_lines"][0]["error"] == "actual"
    )
    catalog, _ = field_catalog(
        [{"evidence_id": "E1", "kind": "query_logs", "errors": [], "data": normalized}]
    )
    assert any(f["path"] == "/parsed_lines/0/error" for f in catalog)
    assert not any(f["path"] == "/lines/0/error" for f in catalog)


def wire(context, claim_change=None):
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        view = json.loads(payload["messages"][1]["content"])
        captured.append((payload, view))
        paths = ["/error", "/known_encoders/bad", "/parsed_lines/0/error"]
        claims = []
        for path in paths:
            field = next(f for f in view["field_catalog"] if f["path"] == path)
            claims.append(
                {
                    "field_id": field["field_id"],
                    "operator": "equals" if field["value_type"] == "boolean" else "contains",
                    "expected": "false" if field["value_type"] == "boolean" else "Unknown encoder",
                }
            )
        if claim_change:
            claim_change(claims, view)
        result = {
            "hypotheses": [
                {
                    "id": "H1",
                    "cause": "FFMPEG_INVALID_CODEC",
                    "explanation": "encoder failure",
                    "search_query": "encoder",
                    "assertions": claims,
                    "required_tools": ["get_task_status", "get_encoder_inventory", "query_logs"],
                }
            ],
            "requests": [],
            "selected_hypothesis_id": "H1",
            "repair_options": [],
            "selected_option_id": None,
            "summary": "investigation",
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(result)}})

    provider = HypothesisModel(
        Settings(_env_file=None, aiops_model_provider="ollama"), httpx.MockTransport(handler)
    )
    return provider.investigate(context), captured


def test_wire_uses_real_fields_and_restores_canonical_evidence_paths():
    rows = observations()
    result, captured = wire(
        {
            "round": 3,
            "rounds_remaining": 1,
            "observations": rows,
            "references": [],
            "prior_assessments": [
                {"hypothesis_id": "H1", "missing_required_tools": ["get_encoder_inventory"]}
            ],
        }
    )
    assert not result._validation_issues
    assert evaluate(result.hypotheses[0], rows)["status"] == "supported"
    claims = result.hypotheses[0].assertions
    assert (
        claims[1].evidence_id == "encoders"
        and claims[1].path == "/known_encoders/bad"
        and claims[1].expected is False
    )
    schema, context = captured[0]
    assert set(schema["format"]["$defs"]["Assertion"]["properties"]) == {
        "field_id",
        "operator",
        "expected",
    }
    assert (
        context["stage"] == "final_evidence_review"
        and schema["format"]["properties"]["requests"]["maxItems"] == 0
    )
    feedback = context["required_tool_feedback"][0]
    assert feedback["available_fields"] and all(
        f["field_id"] in schema["format"]["$defs"]["Assertion"]["properties"]["field_id"]["enum"]
        for f in feedback["available_fields"]
    )
    assert all("data" not in row for row in context["observations"])


@pytest.mark.parametrize("mistake", ["invented", "legacy_path", "wrong_operator", "wrong_boolean"])
def test_bad_field_selection_is_rejected_without_filling_in_proof(mistake):
    def change(claims, view):
        if mistake == "invented":
            claims[0]["field_id"] = "F999999"
        if mistake == "legacy_path":
            claims[0] = {
                "evidence_id": "E1",
                "path": "/lines/0/error",
                "operator": "contains",
                "expected": "Unknown encoder",
            }
        if mistake == "wrong_operator":
            claims[1]["operator"] = "contains"
        if mistake == "wrong_boolean":
            claims[1]["expected"] = "0"

    result, _ = wire({"round": 2, "observations": observations(), "references": []}, change)
    assert result._validation_issues and result.hypotheses[0].assertions == []
    assert evaluate(result.hypotheses[0], observations())["status"] == "unresolved"


def test_server_does_not_replace_hypothesis_prediction_with_observed_value():
    result, _ = wire(
        {"round": 2, "observations": observations(), "references": []},
        lambda claims, view: claims[1].update(expected="true"),
    )
    assert not result._validation_issues
    assert result.hypotheses[0].assertions[1].expected is True
    assert evaluate(result.hypotheses[0], observations())["status"] == "contradicted"


def test_catalog_skips_failed_truncated_empty_and_unsafe_fields_and_is_bounded():
    rows = [
        {
            "evidence_id": "failed",
            "kind": "query_metric",
            "errors": ["timeout"],
            "data": {"value": 0},
        },
        {
            "evidence_id": "truncated",
            "kind": "query_metric",
            "errors": [],
            "truncated": True,
            "data": {"value": 0},
        },
        {"evidence_id": "empty", "kind": "query_metric", "errors": [], "data": {"series": []}},
        {
            "evidence_id": "ok",
            "kind": "query_logs",
            "errors": [],
            "data": {"bad/key": "x", "text": "a" * 1000, **{str(i): i for i in range(500)}},
        },
    ]
    catalog, lookup = field_catalog(rows)
    assert catalog and len(catalog) <= 112
    assert all(f["evidence_id"] == "ok" and "bad/key" not in f["path"] for f in catalog)
    text = next(f for f in catalog if f["path"] == "/text")
    assert (
        text["value_truncated"] and text["operators"] == ["contains"] and len(text["value"]) == 400
    )
    assert sum(len(json.dumps(f, ensure_ascii=False).encode()) for f in catalog) <= 14000
    assert all(f["field_id"] in lookup for f in catalog)


@pytest.mark.parametrize(
    "mode",
    [
        "retain",
        "abstain",
        "omit_tool",
        "contradict",
        "change_scope",
        "wrong_tool",
        "query_metadata",
        "policy_task",
    ],
)
def test_final_audit_only_changes_leading_assertions_and_cannot_fill_missing_proof(mode):
    rows = observations()
    rows[1]["data"].update(available=True, requested_encoder="bad", encoder_count=225)
    first, _ = wire({"round": 2, "observations": rows, "references": []})
    prior = first.model_dump()
    prior["hypotheses"].append(
        {
            "id": "H2",
            "cause": "OTHER_FAILURE",
            "explanation": "retained alternative",
            "search_query": "other",
            "assertions": [],
            "missing_information": ["unresolved"],
        }
    )
    prior["repair_options"] = [
        {
            "id": "P1",
            "hypothesis_id": "H1",
            "action_type": "retry_video_ffmpeg_h264",
            "rationale": "review",
            "risk": "retry",
            "prerequisites": [],
            "verification": "same video",
            "runbook_chunk_ids": ["canonical-runbook"],
        }
    ]
    prior["selected_option_id"] = "P1"
    prior = first.__class__.model_validate(prior).model_dump()
    if mode == "policy_task":
        prior["hypotheses"][0]["required_tools"] = ["get_encoder_inventory"]
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        view = json.loads(payload["messages"][1]["content"])
        captured.append(payload)
        assert set(payload["format"]["properties"]) == {
            "selection",
            "assertions",
            "tool_assertions",
            "summary",
        }
        slot = payload["format"]["properties"]["tool_assertions"]["properties"][
            "get_encoder_inventory"
        ]
        allowed = slot["anyOf"][0]["properties"]["field_id"]["enum"]
        assert all(
            f["path"].startswith("/known_encoders/")
            for f in view["field_catalog"]
            if f["field_id"] in allowed
        )
        claims = []
        slots = {
            tool: None for tool in payload["format"]["properties"]["tool_assertions"]["properties"]
        }
        if mode == "policy_task":
            assert (
                view["policy_evidence_tools"] == ["get_task_status"] and "get_task_status" in slots
            )
        for path in ["/error", "/known_encoders/bad", "/parsed_lines/0/error"]:
            if mode == "omit_tool" and path == "/known_encoders/bad":
                continue
            if mode == "policy_task" and path == "/parsed_lines/0/error":
                continue
            field = next(f for f in view["field_catalog"] if f["path"] == path)
            expected = (
                "true"
                if mode == "contradict" and path == "/known_encoders/bad"
                else "false"
                if field["value_type"] == "boolean"
                else "Unknown encoder"
            )
            slots[field["tool_name"]] = {
                "field_id": field["field_id"],
                "operator": "equals" if field["value_type"] == "boolean" else "contains",
                "expected": expected,
            }
        patch = {
            "selection": "abstain" if mode == "abstain" else "retain",
            "assertions": claims,
            "tool_assertions": slots,
            "summary": "依既有觀測完成查核。",
        }
        if mode == "change_scope":
            patch["required_tools"] = []
        if mode == "wrong_tool":
            slots["get_encoder_inventory"] = slots["get_task_status"]
        if mode == "query_metadata":
            field = next(f for f in view["field_catalog"] if f["path"] == "/requested_encoder")
            slots["get_encoder_inventory"] = {
                "field_id": field["field_id"],
                "operator": "equals",
                "expected": "bad",
            }
        return httpx.Response(200, json={"message": {"content": json.dumps(patch)}})

    provider = HypothesisModel(
        Settings(_env_file=None, aiops_model_provider="ollama"), httpx.MockTransport(handler)
    )
    context = {
        "round": 3,
        "rounds_remaining": 1,
        "observations": rows,
        "references": [],
        "previous_candidate": prior,
    }
    if mode in {"change_scope", "wrong_tool", "query_metadata"}:
        with pytest.raises(ValueError):
            provider.investigate(context)
        return
    result = provider.investigate(context)
    assert len(captured) == 1 and result.requests == []
    assert (
        result.hypotheses[1].model_dump()
        == first.__class__.model_validate(prior).hypotheses[1].model_dump()
    )
    assert result.hypotheses[0].required_tools == prior["hypotheses"][0]["required_tools"]
    assert result.repair_options[0].runbook_chunk_ids == ["canonical-runbook"]
    if mode == "abstain":
        assert result.selected_hypothesis_id is None and result.selected_option_id is None
    else:
        assert result.selected_hypothesis_id == "H1" and result.selected_option_id == "P1"
    status = evaluate(result.hypotheses[0], rows)["status"]
    assert status == (
        "unresolved"
        if mode == "omit_tool"
        else "contradicted"
        if mode == "contradict"
        else "supported"
    )


def test_field_ids_do_not_rebind_when_observations_grow_or_aliases_change():
    from app.aiops.hypothesis_validation import field_assertions

    probe = {
        "evidence_id": "E3",
        "kind": "probe_video_input",
        "errors": [],
        "data": {"streams": [{"codec_name": "h264"}]},
    }
    first = [{**observations()[0], "evidence_id": "E1"}, probe]
    catalog, fields = field_catalog(first, {"E1": "task-guid", "E3": "probe-guid"})
    codec = next(f for f in catalog if f["path"] == "/streams/0/codec_name")
    added = [
        {
            "evidence_id": "E1",
            "kind": "query_logs",
            "errors": [],
            "data": {"parsed_lines": [{"logger": "worker.app.tasks"}]},
        },
        {**probe, "evidence_id": "E2"},
        {**first[0], "evidence_id": "E3"},
    ]
    updated, lookup = field_catalog(
        added, {"E1": "log-guid", "E2": "probe-guid", "E3": "task-guid"}
    )
    same = next(f for f in updated if f["path"] == "/streams/0/codec_name")
    logger = next(f for f in updated if f["path"] == "/parsed_lines/0/logger")
    assert same["field_id"] == codec["field_id"] and logger["field_id"] != codec["field_id"]
    assertion = field_assertions(
        {"assertions": [{"field_id": codec["field_id"], "operator": "equals", "expected": "h264"}]},
        lookup,
    )["assertions"][0]
    assert assertion["evidence_id"] == "E2" and assertion["path"] == "/streams/0/codec_name"
    # Losing a field to failure/budget does not let the old ID refer elsewhere.
    _, missing = field_catalog([added[0]], {"E1": "log-guid"})
    with pytest.raises(ValueError):
        field_assertions(
            {
                "assertions": [
                    {"field_id": codec["field_id"], "operator": "equals", "expected": "h264"}
                ]
            },
            missing,
        )


def test_same_path_in_different_evidence_and_changed_types_have_distinct_ids():
    rows = [
        {"evidence_id": "one", "kind": "query_metric", "errors": [], "data": {"value": 0}},
        {"evidence_id": "two", "kind": "query_metric", "errors": [], "data": {"value": 0}},
        {"evidence_id": "three", "kind": "query_metric", "errors": [], "data": {"value": False}},
    ]
    catalog, _ = field_catalog(rows)
    assert len({f["field_id"] for f in catalog}) == 3
    boolean, _ = field_catalog([{**rows[0], "data": {"value": False}}])
    assert boolean[0]["field_id"] != catalog[0]["field_id"]
