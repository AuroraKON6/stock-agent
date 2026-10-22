"""Agent intent protocol: validated JSON intents executed by a Python broker.

Plan W07 / brief section 5: the model never runs tools. It emits a JSON
research intent; this module validates it against a strict schema and an
action allowlist, and the broker executes only allowed, auditable reads
against the governed public context attached to the task. Free-form command
strings, arbitrary URLs, shell access, and arbitrary filesystem paths are
rejected before anything runs, and the external-facing prompt payload is
minimized (no roster, no restriction lists, no local paths, no secrets).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

AGENT_PROTOCOL_VERSION = "1.0"

INTENT_SCHEMA_VERSION = "1.0"
OUTPUT_SCHEMA_VERSION = "1.0"

ALLOWED_ACTIONS = (
    "read_public_quote_snapshot",
    "read_public_news_cluster",
    "read_verified_evidence_summary",
    "calculate_approved_indicator",
    "propose_research_subtask",
    "request_human_evidence",
    "draft_research_note",
)

APPROVED_INDICATORS = ("pct_change", "absolute_change", "simple_average")

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9:.\-]{1,16}$")
_EVIDENCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{4,128}$")
_FORBIDDEN_FRAGMENTS = ("http://", "https://", "file://", "\\\\", ":\\", "..", "$(", "<", ">")

REQUIRED_INTENT_FIELDS = (
    "schema_version",
    "task_id",
    "action",
    "parameters",
    "reason_summary",
)

REQUIRED_OUTPUT_FIELDS = (
    "schema_version",
    "task_id",
    "summary",
    "human_questions",
)


class IntentValidationError(ValueError):
    pass


def _reject_forbidden(value: Any, label: str) -> str:
    text = str(value or "")
    lowered = text.lower()
    for fragment in _FORBIDDEN_FRAGMENTS:
        if fragment in lowered:
            raise IntentValidationError(f"{label} must not contain the forbidden fragment")
    return text


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IntentValidationError(message)


def _validate_symbol(value: Any) -> str:
    text = _reject_forbidden(value, "parameters.symbol")
    _require(bool(_SYMBOL_PATTERN.fullmatch(text)), "parameters.symbol has an unsupported format")
    return text


def validate_intent(intent: Any) -> Dict[str, Any]:
    """Validate one model intent; raise IntentValidationError on anything unsafe."""

    _require(isinstance(intent, dict), "intent must be a JSON object")
    for field in REQUIRED_INTENT_FIELDS:
        _require(field in intent, "intent is missing a required field")
    _require(str(intent.get("schema_version") or "") == INTENT_SCHEMA_VERSION,
             "intent schema_version is not supported")
    task_id = str(intent.get("task_id") or "")
    _require(bool(task_id) and len(task_id) <= 64, "intent task_id must be 1-64 chars")
    _reject_forbidden(task_id, "intent.task_id")
    action = str(intent.get("action") or "")
    _require(action in ALLOWED_ACTIONS, "intent action is not in the allowlist")
    parameters = intent.get("parameters")
    _require(isinstance(parameters, dict), "intent parameters must be an object")
    for key, value in parameters.items():
        _require(isinstance(key, str) and key and len(key) <= 32, "parameter names must be short strings")
        _require(isinstance(value, (str, int, float, bool, list, dict)), "parameter values must be JSON scalars or containers")
        if isinstance(value, str):
            _reject_forbidden(value, "a parameter value")
    reason = str(intent.get("reason_summary") or "")
    _require(bool(reason.strip()), "reason_summary must not be empty")
    _require(len(reason) <= 2000, "reason_summary is too long")
    _reject_forbidden(reason, "reason_summary")
    evidence_refs = intent.get("evidence_refs", [])
    _require(isinstance(evidence_refs, list), "evidence_refs must be a list")
    for ref in evidence_refs:
        text = str(ref or "")
        _require(bool(_EVIDENCE_REF_PATTERN.fullmatch(text)), "an evidence ref has an unsupported format")
    stop = intent.get("stop_condition", "")
    if stop:
        _require(len(str(stop)) <= 500, "stop_condition is too long")
        _reject_forbidden(stop, "stop_condition")
    expected = intent.get("expected_output", "")
    if expected:
        _require(len(str(expected)) <= 500, "expected_output is too long")
        _reject_forbidden(expected, "expected_output")
    return dict(intent)


def validate_model_output(output: Any, allowed_evidence_refs: Optional[List[str]] = None) -> Dict[str, Any]:
    """Validate the research-note payload a model must return for one task.

    When ``allowed_evidence_refs`` is provided, every evidence reference in
    the output must belong to the CURRENT task's approved context; unknown,
    foreign-case, or stale references are rejected before anything is stored.
    """

    _require(isinstance(output, dict), "model output must be a JSON object")
    for field in REQUIRED_OUTPUT_FIELDS:
        _require(field in output, "model output is missing a required field")
    _require(str(output.get("schema_version") or "") == OUTPUT_SCHEMA_VERSION,
             "output schema_version is not supported")
    summary = str(output.get("summary") or "")
    _require(bool(summary.strip()), "output summary must not be empty")
    _require(len(summary) <= 4000, "output summary is too long")
    questions = output.get("human_questions")
    _require(isinstance(questions, list), "human_questions must be a list")
    for question in questions:
        _require(isinstance(question, dict), "each human question must be an object")
        _require(bool(str(question.get("question") or "").strip()), "each human question needs question text")
    for field in ("key_findings", "unknowns", "evidence_refs"):
        value = output.get(field, [])
        _require(isinstance(value, list), "an output list field has an invalid type")
    if allowed_evidence_refs is not None:
        allowed = {str(ref) for ref in allowed_evidence_refs}
        for ref in output.get("evidence_refs", []):
            _require(str(ref) in allowed,
                     "output cites evidence that is not part of the current approved context")
    confidence = output.get("model_self_assessed_confidence")
    if confidence is not None:
        try:
            numeric = float(confidence)
        except (TypeError, ValueError) as exc:
            raise IntentValidationError("confidence must be numeric") from exc
        _require(0.0 <= numeric <= 1.0, "confidence must be within the unit interval")
    return dict(output)


class ProtocolBroker:
    """Run validated intents against the governed public context only.

    Dispatch is an explicit branch over validated action names: there is no
    dynamic handler lookup driven by model input.
    """

    def __init__(self, public_context: Optional[Dict[str, Any]] = None):
        self.public_context = public_context or {}

    def execute_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        validated = validate_intent(intent)
        action = validated["action"]
        parameters = validated["parameters"]
        if action == "read_public_quote_snapshot":
            result = self._read_public_quote_snapshot(parameters)
        elif action == "read_public_news_cluster":
            result = self._read_public_news_cluster(parameters)
        elif action == "read_verified_evidence_summary":
            result = self._read_verified_evidence_summary(parameters)
        elif action == "calculate_approved_indicator":
            result = self._calculate_approved_indicator(parameters)
        elif action == "propose_research_subtask":
            result = self._propose_research_subtask(parameters)
        elif action == "request_human_evidence":
            result = self._request_human_evidence(parameters)
        elif action == "draft_research_note":
            result = self._draft_research_note(parameters)
        else:  # defensive: validate_intent already enforces the allowlist
            raise IntentValidationError("intent action is not in the allowlist")
        return {
            "schema_version": AGENT_PROTOCOL_VERSION,
            "action": action,
            "status": "ok",
            "result": result,
        }

    def _read_public_quote_snapshot(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        symbol = _validate_symbol(parameters.get("symbol"))
        quotes = self.public_context.get("quote_snapshots") or {}
        _require(symbol in quotes, "requested symbol is not part of the approved public context")
        return {"symbol": symbol, "snapshot": quotes[symbol]}

    def _read_public_news_cluster(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        cluster_id = str(parameters.get("cluster_id") or "")
        _require(bool(_EVIDENCE_REF_PATTERN.fullmatch(cluster_id)), "cluster_id has an unsupported format")
        clusters = self.public_context.get("news_clusters") or {}
        _require(cluster_id in clusters, "requested news cluster is not part of the approved public context")
        return {"cluster_id": cluster_id, "cluster": clusters[cluster_id]}

    def _read_verified_evidence_summary(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        evidence_ref = str(parameters.get("evidence_ref") or "")
        _require(bool(_EVIDENCE_REF_PATTERN.fullmatch(evidence_ref)), "evidence_ref has an unsupported format")
        summaries = self.public_context.get("evidence_summaries") or {}
        _require(evidence_ref in summaries, "requested evidence is not part of the approved public context")
        return {"evidence_ref": evidence_ref, "summary": summaries[evidence_ref]}

    def _calculate_approved_indicator(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        name = str(parameters.get("indicator") or "")
        _require(name in APPROVED_INDICATORS, "indicator is not approved")
        series = parameters.get("series")
        _require(isinstance(series, list) and len(series) >= 2 and all(isinstance(v, (int, float)) for v in series),
                 "indicator requires a numeric series with at least two points")
        if name == "pct_change":
            first, last = float(series[0]), float(series[-1])
            _require(first != 0, "pct_change requires a non-zero base")
            return {"indicator": name, "value": (last - first) / abs(first) * 100.0}
        if name == "absolute_change":
            return {"indicator": name, "value": float(series[-1]) - float(series[0])}
        return {"indicator": name, "value": sum(float(v) for v in series) / len(series)}

    def _propose_research_subtask(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        question = str(parameters.get("question") or "")
        _require(bool(question.strip()), "subtask proposal requires a question")
        _require(len(question) <= 500, "subtask question is too long")
        return {"proposed_subtask": {"question": question, "status": "proposal_only", "requires_operator_approval": True}}

    def _request_human_evidence(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        question = str(parameters.get("question") or "")
        _require(bool(question.strip()), "evidence request requires a question")
        _require(len(question) <= 500, "evidence request question is too long")
        reason = str(parameters.get("reason") or "")[:500]
        return {"human_evidence_request": {"question": question, "reason": reason}}

    def _draft_research_note(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        note = str(parameters.get("note") or "")
        _require(bool(note.strip()), "draft note must not be empty")
        _require(len(note) <= 8000, "draft note is too long")
        return {"research_note_draft": note, "status": "draft_pending_human_review"}


# R06: claim extraction shared with the research-quality contract. The
# protocol package stays the stable surface callers import from; the logic
# lives in research_quality so worker and future façades share one scorer.
def claim_texts(output: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a validated model output into {source, text, refs} claims."""
    from ..research_quality import claim_texts as _claim_texts

    return _claim_texts(output)
