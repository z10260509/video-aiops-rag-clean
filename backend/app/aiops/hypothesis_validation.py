"""Isolate invalid hypotheses without accepting their assertions or hiding failures."""

import json
import re
from pydantic import ValidationError
from app.aiops.hypotheses import Hypothesis, RepairOption, RoundOutput


class FieldSelectionError(ValueError):
    def __init__(self, code, index):
        self.code = code
        self.loc = ["assertions", index]


def field_assertions(row, fields):
    row = dict(row)
    assertions = row.get("assertions", [])
    if not isinstance(assertions, list):
        return row
    converted = []
    for index, claim in enumerate(assertions):
        if not isinstance(claim, dict) or set(claim) != {"field_id", "operator", "expected"}:
            raise FieldSelectionError("field_selection_required", index)
        field = fields.get(claim["field_id"]) if isinstance(claim["field_id"], str) else None
        if field is None:
            raise FieldSelectionError("unknown_field", index)
        if claim["operator"] not in field["operators"]:
            raise FieldSelectionError("operator_type_mismatch", index)
        if not isinstance(claim["expected"], str):
            raise FieldSelectionError("expected_text_required", index)
        converted.append(
            {k: field[k] for k in ("evidence_id", "path", "value_type")}
            | {"operator": claim["operator"], "expected": claim["expected"]}
        )
    row["assertions"] = converted
    return row


def validation_details(exc, prefix=()):
    if isinstance(exc, FieldSelectionError):
        messages = {
            "field_selection_required": "請只填 field_id、operator、expected；證據、路徑及型別由系統綁定。",
            "unknown_field": "欄位不存在或不能使用；請從本輪 field_catalog 選擇現有 field_id。",
            "operator_type_mismatch": "運算子不適用於此欄位型別；請使用該欄位列出的 operators。",
            "expected_text_required": "expected 必須是文字格式的預期值，布林使用 true／false，數字使用十進位。",
        }
        return [{"loc": list(prefix) + exc.loc, "type": exc.code, "message": messages[exc.code]}]
    if not isinstance(exc, ValidationError):
        return [
            {
                "loc": list(prefix),
                "type": "invalid_reference_or_selection",
                "message": "Check supplied evidence/runbook aliases and stable hypothesis IDs.",
            }
        ]
    messages = {
        "Finite number required": "預期值必須是有限數字，不能使用 true／false。",
        "Boolean assertion requires true/false": "布林條件只能使用 true 或 false，請對照原觀測型別。",
        "String assertion required": "字串條件必須提供文字。",
        "Read-only queries belong in requests, not repair_options": "唯讀查詢必須放在調查 requests，不能當作修復方案。",
    }
    result = []
    for error in exc.errors(include_input=False)[:6]:
        # Never echo arbitrary invalid model output, credentials or exception text.
        reason = str((error.get("ctx") or {}).get("error", ""))
        result.append(
            {
                "loc": list(prefix) + list(error["loc"]),
                "type": error["type"],
                "message": messages.get(reason, "欄位格式不符，請依 schema 與原觀測重新填寫。"),
            }
        )
    return result


def parse_round(value, evidence_map=None, reference_map=None, fields=None):
    raw = json.loads(value) if isinstance(value, str) else value
    if not isinstance(raw, dict):
        return RoundOutput.model_validate(raw)
    raw = dict(raw)
    rows = raw.get("hypotheses")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 3:
        return RoundOutput.model_validate(raw)
    hypotheses = []
    issues = []
    for index, row in enumerate(rows):
        try:
            h = Hypothesis.model_validate(
                field_assertions(row, fields)
                if fields is not None and isinstance(row, dict)
                else row
            )
            if evidence_map is not None:
                for assertion in h.assertions:
                    if assertion.evidence_id not in evidence_map:
                        raise ValueError("Unknown evidence alias")
                    assertion.evidence_id = evidence_map[assertion.evidence_id]
        except (ValidationError, ValueError) as exc:
            # Preserve identity, never old assertions. Invalid identity/cause rejects
            # the envelope: it cannot safely be attached to an existing hypothesis.
            identity = {k: row.get(k) for k in ("id", "cause")} if isinstance(row, dict) else {}
            h = Hypothesis(
                **identity,
                explanation="本輪假說格式錯誤，尚未驗證。",
                assertions=[],
                missing_information=["修正本輪假說格式後重新查核。"],
                search_query="hypothesis validation",
            )
            for error in validation_details(exc, ("hypotheses", index)):
                issue = {**error, "hypothesis_id": h.id}
                loc = error["loc"][2:]
                if len(loc) > 1 and loc[0] == "assertions" and isinstance(loc[1], int):
                    assertions = row.get("assertions", [])
                    if (
                        isinstance(assertions, list)
                        and loc[1] < len(assertions)
                        and isinstance(assertions[loc[1]], dict)
                    ):
                        path = assertions[loc[1]].get("path")
                        if isinstance(path, str) and re.fullmatch(r"/[a-zA-Z0-9_/-]{1,180}", path):
                            issue["path"] = path
                issues.append(issue)
        hypotheses.append(h.model_dump())
    raw["hypotheses"] = hypotheses
    plans = raw.get("repair_options", [])
    if isinstance(plans, list) and len(plans) <= 3:
        validated = []
        for index, row in enumerate(plans):
            try:
                plan = RepairOption.model_validate(row)
                if reference_map is not None:
                    if any(cid not in reference_map for cid in plan.runbook_chunk_ids):
                        raise ValueError("Unknown runbook alias")
                    plan.runbook_chunk_ids = [reference_map[cid] for cid in plan.runbook_chunk_ids]
            except (ValidationError, ValueError) as exc:
                identity = (
                    {k: row.get(k) for k in ("id", "hypothesis_id")}
                    if isinstance(row, dict)
                    else {}
                )
                # Preserve selection identity, never replace the chosen option with
                # another or resurrect last round's plan. This sentinel has no executor.
                plan = RepairOption(
                    **identity,
                    action_type="invalid_repair_option",
                    rationale="本輪方案格式或引用錯誤，不能執行。",
                    risk="尚未通過方案檢查。",
                    prerequisites=["修正本輪方案後重新檢查。"],
                    verification="尚未確認。",
                    runbook_chunk_ids=[],
                )
                issues.extend(
                    {**error, "option_id": plan.id}
                    for error in validation_details(exc, ("repair_options", index))
                )
            validated.append(plan.model_dump())
        raw["repair_options"] = validated
    candidate = RoundOutput.model_validate(raw)
    candidate._validation_issues = issues
    return candidate
