"""One bounded structured call per investigation round; no tool execution here."""

import json
from app.aiops.hypotheses import RoundOutput, Hypothesis
from app.aiops.knowledge import REFERENCE_POLICY
from app.aiops.model_client import ModelClient
from app.aiops.gateway import sanitize
from app.aiops.hypothesis_validation import parse_round, field_assertions
from app.aiops.observation_fields import field_catalog


class HypothesisModel(ModelClient):
    def investigate(self, context):
        schema = RoundOutput.model_json_schema()
        schema["required"] = list(schema["properties"])
        for definition in schema.get("$defs", {}).values():
            if "properties" in definition:
                definition["required"] = list(definition["properties"])
        schema["$defs"]["Hypothesis"]["properties"]["required_tools"]["description"] = (
            "Tools whose results MUST substantiate this hypothesis. For every required gap, list its resolving tool. A tool is satisfied only by an actual supported assertion citing its result. Encoder-list gap: get_encoder_inventory; input gap: probe_video_input."
        )
        schema["$defs"]["Hypothesis"]["properties"]["missing_information"]["description"] = (
            "Only missing proof REQUIRED to validate immediate-failure assertions. Optional configuration/version/history goes into follow_up_checks. Clear resolved gaps after tool results."
        )
        if context.get("rounds_remaining", 3) <= 1:
            schema["properties"]["requests"]["maxItems"] = 0
        # Use short aliases for small local models, expanding them before persistence.
        context = dict(context)
        evidence_map = {}
        reference_map = {}
        observations = []
        for i, row in enumerate(context.pop("observations", []), 1):
            alias = "E" + str(i)
            evidence_map[alias] = row["evidence_id"]
            observations.append({**row, "evidence_id": alias})
        references = []
        for i, row in enumerate(context.pop("references", []), 1):
            alias = "R" + str(i)
            reference_map[alias] = row["chunk_id"]
            references.append({**row, "chunk_id": alias})
        schema["$defs"]["Assertion"]["properties"]["evidence_id"]["enum"] = list(evidence_map) or [
            "NO_EVIDENCE"
        ]
        if not reference_map:
            schema["properties"]["repair_options"]["maxItems"] = 0
            schema["properties"]["selected_option_id"] = {"type": "null"}
        if context["round"] == 1:
            schema["properties"]["selected_hypothesis_id"] = {"type": "null"}
        if reference_map:
            schema["$defs"]["RepairOption"]["properties"]["runbook_chunk_ids"].update(
                minItems=1, items={"type": "string", "enum": list(reference_map)}
            )
        prior = context.pop("previous_candidate", None)
        if prior:
            context["previous_hypotheses"] = [
                {
                    k: h[k]
                    for k in [
                        "id",
                        "cause",
                        "missing_information",
                        "required_tools",
                        "follow_up_checks",
                    ]
                }
                for h in prior.get("hypotheses", [])
            ]
        context["review_tasks"] = [
            {
                "hypothesis_id": a["hypothesis_id"],
                "instruction": "All stated assertions are tool-supported. Reassess remaining gaps: move optional configuration/history investigation to follow_up_checks; keep only genuinely missing immediate-failure proof in missing_information. Do not repeat a completed query.",
            }
            for a in context.get("prior_assessments", [])
            if a.get("checks") and all(c["status"] == "supported" for c in a["checks"])
        ]
        context["tool_catalog"] = {
            "get_encoder_inventory": "Read the ORIGINAL WORKER encoder inventory snapshot. Resolves supported-encoder-list gaps; shows known_encoders bool values and original task identity.",
            "probe_video_input": "Read-only FFprobe of original upload; readable boolean, stream codec/dimensions. Resolves input-parsing hypothesis, not full frame integrity.",
            "get_task_status": "Original failed task status and stderr. Already present after initial collection; repeated identical queries use the same snapshot.",
            "get_pod_status": "Pod UID, actual termination and restart details. A running object is not a boolean and does not disprove a past OOM.",
            "query_metric": "Time-series metric_key memory/oom_kills/worker_available/failure_total. Empty series means unknown.",
            "query_logs": "Structured logs scoped to original Job/task; use only if original task stderr is insufficient.",
            "get_pod_events": "Kubernetes events scoped to original Pod UID. Does not inspect Redis or encoder configuration.",
            "get_queue_depth": "Queue metric, not direct encoder evidence.",
        }
        catalog, fields = field_catalog(observations, evidence_map)
        schema["$defs"]["Assertion"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "field_id": {"type": "string", "enum": list(fields) or ["NO_FIELD"]},
                "operator": {"type": "string", "enum": ["equals", "contains", "greater_than"]},
                "expected": {"type": "string", "maxLength": 1200},
            },
            "required": ["field_id", "operator", "expected"],
        }
        context["field_catalog"] = catalog
        context["field_catalog_version"] = "scalar-catalog-v2-stable"
        for correction in (context.get("model_feedback") or {}).get("assertion_corrections", []):
            correction["available_field_ids"] = [
                f["field_id"]
                for f in catalog
                if evidence_map.get(f["evidence_id"]) == correction.get("evidence_id")
                and f["path"] == correction.get("path")
            ]
        context["required_tool_feedback"] = []
        for assessment in context.get("prior_assessments", []):
            for needed in assessment.get("missing_required_tools", []):
                available = [
                    r["evidence_id"]
                    for r in observations
                    if r["kind"] == needed and not r.get("errors")
                ]
                context["required_tool_feedback"].append(
                    {
                        "hypothesis_id": assessment["hypothesis_id"],
                        "required_tool": needed,
                        "available_evidence_aliases": available,
                        "available_fields": [
                            {k: f[k] for k in ("field_id", "path", "value_type", "operators")}
                            for f in catalog
                            if f["evidence_id"] in available
                        ],
                        "instruction": "The tool already returned data. Select a relevant available_fields field_id and assert your hypothesis against it. Do NOT query again; do not remove a required tool merely to pass validation."
                        if available
                        else "Request this missing tool if still necessary.",
                    }
                )
        completed = {r["kind"] for r in observations if not r.get("errors")}
        fixed = {"get_task_status", "get_pod_status", "get_encoder_inventory", "probe_video_input"}
        choices = schema["$defs"]["InvestigationRequest"]["properties"]["tool_name"]["enum"]
        schema["$defs"]["InvestigationRequest"]["properties"]["tool_name"]["enum"] = [
            name for name in choices if name not in completed or name not in fixed
        ]

        # Prior round IDs remain original; provide explicit alias translation for the next response.
        context["stage"] = (
            "initial_hypotheses"
            if context["round"] == 1
            else "final_evidence_review"
            if context.get("rounds_remaining", 3) <= 1
            else "reassess_after_retrieval_and_tools"
        )
        context["task"] = (
            "Choose assertions only through this round field_catalog IDs and permitted operators. expected is the value predicted by YOUR hypothesis, not automatically the observed value. Correct model_feedback and cover each required tool with a meaningful field assertion. Existing observations remain available; never repeat a query just to fix formatting. In final_evidence_review, prioritize verifying the leading hypothesis and its required citations, retain alternatives/contradictions and requests=[], and select none if proof remains incomplete."
        )
        # Scalar fields are the canonical model view. Raw observations are retained
        # in Evidence; avoid duplicating large log strings and hiding later fields.
        compact = [
            {k: r[k] for k in ("evidence_id", "kind", "source", "errors", "truncated") if k in r}
            for r in observations
        ]
        context.update(
            observations=compact,
            references=references,
            evidence_aliases=evidence_map,
            reference_aliases=reference_map,
        )
        focused = bool(
            context["stage"] == "final_evidence_review"
            and prior
            and prior.get("selected_hypothesis_id")
        )
        if focused:
            leading = next(
                h for h in prior["hypotheses"] if h["id"] == prior["selected_hypothesis_id"]
            )
            from app.aiops.actions import SPECS

            plan = next(
                (p for p in prior["repair_options"] if p["id"] == prior.get("selected_option_id")),
                None,
            )
            spec = SPECS.get(plan["action_type"], {}) if plan else {}
            # Mirror the existing repair proof requirements without choosing an
            # action or weakening repair_eligible(). The model must cite them.
            policy_tools = ["get_task_status"] if spec.get("namespace") == "video-platform" else []
            if policy_tools and spec.get("root") == "WORKER_OOM":
                policy_tools.append("get_pod_status")
            audit_tools = list(dict.fromkeys(leading["required_tools"] + policy_tools))
            # Transport/identity fields confirm collection, not the hypothesis.
            metadata = {
                "available",
                "job_id",
                "task_id",
                "incident_id",
                "namespace",
                "pod",
                "pod_uid",
                "source",
                "requested_at",
                "observed_at",
                "timestamp",
                "collected_at",
                "checksum",
                "version",
                "requested_encoder",
                "probe",
                "scope",
                "execution_started_at",
            }
            # Inventory's measured availability lives in known_encoders; the
            # echoed requested encoder is query input, not an availability result.
            # Encoder names/values remain dynamic, with no diagnosis selection.
            tool_fields = {
                tool: [
                    f["field_id"]
                    for f in catalog
                    if f["tool_name"] == tool
                    and f["path"].split("/")[1] not in metadata
                    and (
                        tool != "get_encoder_inventory" or f["path"].startswith("/known_encoders/")
                    )
                ]
                for tool in audit_tools
            }
            tool_properties = {}
            for tool, ids in tool_fields.items():
                claim = json.loads(json.dumps(schema["$defs"]["Assertion"]))
                claim["properties"]["field_id"]["enum"] = ids or ["NO_FIELD"]
                tool_properties[tool] = (
                    {"anyOf": [claim, {"type": "null"}]} if ids else {"type": "null"}
                )
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "selection": {"type": "string", "enum": ["retain", "abstain"]},
                    "assertions": {
                        "type": "array",
                        "maxItems": 6,
                        "items": schema["$defs"]["Assertion"],
                    },
                    "tool_assertions": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": tool_properties,
                        "required": list(tool_properties),
                    },
                    "summary": {"type": "string", "minLength": 1, "maxLength": 600},
                },
                "required": ["selection", "assertions", "tool_assertions", "summary"],
            }
            # All observations remain visible. Only the final output is smaller:
            # it cannot change root cause, required tools, other hypotheses or plans.
            context["leading_hypothesis"] = leading
            context["selected_repair"] = plan
            context["policy_evidence_tools"] = policy_tools
            context["required_tool_feedback"] = [
                f for f in context["required_tool_feedback"] if f["hypothesis_id"] == leading["id"]
            ]
            context["task"] = (
                "Final audit of ONLY leading_hypothesis. Put one discriminating assertion in EACH tool_assertions tool slot, using a field from that tool. Use null if proof is unavailable; null cannot validate that tool. Put other necessary conditions in assertions. The combined list must have at most 6 unique assertions. Do not repeat a completed query. Keep required_tools unchanged. selection=retain only if all necessary proof is available; otherwise abstain. expected remains YOUR prediction, never a server-generated match. Other hypotheses, prior contradictions and repair options stay unchanged."
            )
        content = json.dumps(sanitize(context), ensure_ascii=False)
        if len(content.encode()) > 40000:
            raise ValueError("Investigation context too large")
        messages = [
            {
                "role": "system",
                "content": REFERENCE_POLICY
                + """ You investigate an incident, not execute repairs.
Executable action names are capabilities, NEVER evidence of a cause. Do not invent Redis or OOM dependencies to explain an encoder error.
Propose up to 3 competing hypotheses from observations without a preselected root cause. Use stable H1/H2/H3 IDs across rounds; explain changes.
Write explanation, summary, purpose, missing_information, rationale, risk and verification in Traditional Chinese.
For each hypothesis request a discriminating runbook search, including counterevidence, using search_query.
Always provide every schema field, using null or [] only when truly inapplicable.
For the leading hypothesis, write at least two concrete assertions if relevant observations exist, Select existing field_catalog field_id values; expected is a string containing the hypothesis prediction. A second assertion might check a specific error substring with contains. Assertions must substantiate the cause, not merely check job_present or a healthy phase.
For every required missing question also list its resolving tool in required_tools. At least ONE relevant assertion must cite EACH required tool result. For example if probe_video_input is required, select its readable field_id, operator equals and expected="true" if that is the prediction being checked. Merely listing the tool in required_tools is not a citation.
Actual assertions citing that tool establish whether the question has been answered; prose is not proof. Do not require unrelated tools for a directly observed error. For an unobserved alternative, list the missing evidence and request a discriminating tool rather than inventing assertions. A healthy Pod today does not contradict a past OOM.
Field types are server-owned. Boolean expected is "false" or "true"; numeric expected is a decimal string. Use only a listed operator. Never compare objects to booleans or interpret numeric 0 as false.
Assertions contain ONLY field_id, operator, expected. The server resolves evidence, path and type from field_catalog; do not write evidence_id/path/value_type yourself. query_logs has parsed_lines extracted from original structured logs; its raw line remains separate. Fields absent from the catalog or failed tools are unknown, never proof of absence.
Do not assert confidence as fact. Do not use runbook text as observed evidence. Retain contradicted hypotheses and describe them; do not discard inconvenient evidence.
The server prioritizes unqueried fixed checks declared in the leading hypothesis required_tools within the same tool budget. You must still cite their actual returned fields in later assertions; scheduling is not proof. Metric/log queries require an explicit request with allowed query parameters; the server never invents those selectors.
Request up to 2 allowed read-only tools to resolve missing information. Tools have server-owned job/pod targets. Never supply shell, SQL, URLs or alternate IDs.
First round: produce hypotheses and useful requests; don't claim a final diagnosis before searching runbooks.
The same item must never appear in BOTH missing_information and follow_up_checks: explicitly decide whether it is required or optional.
Separate direct-failure proof from optional configuration/history investigation: missing_information only contains missing immediate-failure proof, while follow_up_checks contains optional deployment/version/configuration investigation. A complete worker snapshot known_encoders answers encoder availability; do not keep asking for the full list. Cite R aliases in EVERY proposed repair option; do not copy last round unchanged.
Later rounds: explicitly update assertion citations, missing_information and selections after reading prior_assessments, observations and references. Do not leave a supported leading cause with assertions=[] or keep asking only for runbook retrieval. Select a hypothesis only when sufficient observations support all its assertions and no required information is missing. Prefer checks that distinguish it from alternatives.
Repair options must change or recover the failed service/task. Read-only tools such as get_encoder_inventory are investigation requests, NEVER repair options. Do not select a query already completed as a repair. Put optional further investigation in follow_up_checks. Compare actual repair options with risks, prerequisites and recovery checks; references use supplied R aliases. An advisory repair may be described but only server-listed actions are executable. Choose none when no repair is supported.
Select an option only if supported by cited runbooks. Selecting none or an advisory option is valid; unknown causes require human investigation.
End investigation with requests=[] when done or no meaningful query remains. Never approve or execute. Output exactly the requested JSON schema."""
                + json.dumps(schema, ensure_ascii=False),
            },
            {"role": "user", "content": content},
        ]
        if focused:
            messages[0]["content"] = (
                REFERENCE_POLICY
                + """ Final evidence audit, not execution or a new diagnosis.
Write summary in Traditional Chinese. Output only selection, assertions, tool_assertions, summary according to the schema.
Choose only actual field_catalog field_id values and permitted operators. expected must be a string prediction of the hypothesis.
For EVERY tool slot fill a meaningful assertion from that tool, or null if unavailable; identity/query metadata is not causal proof. Slots include hypothesis requirements AND selected-repair policy_evidence_tools. Cite the original failed task error/status and, for OOM repair, its actual Pod termination. Encoder inventory cannot replace original task evidence. Other assertions belong in assertions. Use at most 6 unique assertions combined.
Use existing observations. Do not invent absent fields, convert 0 to false, or treat unavailable/empty results as absence.
If observations do not support the hypothesis or a required proof is unavailable, choose abstain; never approve or execute.
No changing cause, dropping required tools, selecting other repair options or erasing prior contradictions.
"""
                + json.dumps(schema, ensure_ascii=False)
            )
        headers = {}
        if self.hosted:
            key = self.settings.aiops_model_api_key.get_secret_value()
            if not key:
                raise ValueError("Model API key missing")
            headers = {"Authorization": "Bearer " + key}
            payload = {
                "model": self.name,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 3600,
                "response_format": {"type": "json_object"},
            }
        else:
            payload = {
                "model": self.name,
                "messages": messages,
                "format": schema,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_predict": 3600,
                    "num_ctx": min(32768, max(8192, self.settings.aiops_model_context_tokens)),
                },
            }
        with self.client.stream(
            "POST",
            self.base + ("/chat/completions" if self.hosted else "/api/chat"),
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            for part in response.iter_bytes(chunk_size=4096):
                raw.extend(part)
                if len(raw) > 65536:
                    raise ValueError("Investigation response too large")
        body = json.loads(raw)
        response = (
            body["choices"][0]["message"]["content"] if self.hosted else body["message"]["content"]
        )
        if focused:
            patch = json.loads(response)
            if (
                not isinstance(patch, dict)
                or set(patch) != {"selection", "assertions", "tool_assertions", "summary"}
                or not isinstance(patch["selection"], str)
                or patch["selection"] not in {"retain", "abstain"}
                or not isinstance(patch["summary"], str)
                or not 1 <= len(patch["summary"]) <= 600
            ):
                raise ValueError("Invalid final audit envelope")
            slots = patch["tool_assertions"]
            if (
                not isinstance(slots, dict)
                or set(slots) != set(tool_fields)
                or not isinstance(patch["assertions"], list)
            ):
                raise ValueError("Invalid final audit tool slots")
            claims = list(patch["assertions"])
            for tool, claim in slots.items():
                if claim is None:
                    continue
                if not isinstance(claim, dict) or claim.get("field_id") not in tool_fields[tool]:
                    raise ValueError("Final audit tool field mismatch")
                if claim not in claims:
                    claims.append(claim)
            updated = Hypothesis.model_validate(
                field_assertions({**leading, "assertions": claims}, fields)
            )
            for claim in updated.assertions:
                claim.evidence_id = evidence_map[claim.evidence_id]
            merged = {
                **prior,
                "hypotheses": [
                    updated.model_dump() if h["id"] == updated.id else h
                    for h in prior["hypotheses"]
                ],
                "requests": [],
                "summary": patch["summary"],
            }
            if patch["selection"] == "abstain":
                merged.update(selected_hypothesis_id=None, selected_option_id=None)
            candidate = RoundOutput.model_validate(merged)
            candidate._validation_issues = [
                e
                for e in (context.get("model_feedback") or {}).get("issues", [])
                if e.get("hypothesis_id") != updated.id
            ]
            return candidate
        return parse_round(response, evidence_map, reference_map, fields)


class UnavailableHypothesisModel:
    """Fake provider never silently substitutes a rule-based diagnosis in v2."""

    model_id = "fake-hypothesis-disabled"

    def investigate(self, context):
        raise ValueError("Hypothesis v2 requires an actual model or explicit test double")


def hypothesis_model(settings):
    return (
        UnavailableHypothesisModel()
        if settings.aiops_model_provider == "fake"
        else HypothesisModel(settings)
    )
