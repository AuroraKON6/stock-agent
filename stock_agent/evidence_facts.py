"""GLM-03 typed facts bound to verified excerpts; deterministic numeric math.

A fact is only as good as its binding: every fact carries the excerpt
selector (document, character range, excerpt hash), the parser version that
produced the text, and its fact type. Numeric facts declare units; derived
values record the deterministic formula and input fact ids so no value ever
relies on model arithmetic. Facts stay local-review-only and never become
externally eligible here.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from .evidence_excerpts import verify_excerpt_selection
from .event_contract import AMOUNT_UNITS, UNIT_TO_CNY_FACTOR

FACT_TYPES = {"statement", "amount", "ratio", "count", "date", "identifier"}
MAX_STATEMENT_CHARS = 2000


class EvidenceFactError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_computation(computation: Any) -> Dict[str, Any]:
    if not isinstance(computation, dict):
        raise EvidenceFactError("computation must be an object")
    formula = computation.get("formula")
    input_ids = computation.get("input_fact_ids")
    if not isinstance(formula, str) or not formula.strip() or len(formula) > 500:
        raise EvidenceFactError("computation.formula must be a 1..500 character string")
    if (not isinstance(input_ids, list) or not input_ids
            or not all(isinstance(item, str) and item.startswith("fact:") for item in input_ids)):
        raise EvidenceFactError(
            "computed facts must reference at least one existing fact: input_fact_ids")
    extra = set(computation) - {"formula", "input_fact_ids"}
    if extra:
        raise EvidenceFactError("unexpected computation fields: {0}".format(sorted(extra)))
    return {"formula": formula, "input_fact_ids": list(input_ids),
            "converted_by": "deterministic_code"}


def build_fact(
    config: Dict[str, Any],
    selector: Dict[str, Any],
    *,
    fact_type: str,
    statement: str,
    value: Optional[float] = None,
    unit: Optional[str] = None,
    computed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if fact_type not in FACT_TYPES:
        raise EvidenceFactError("fact_type must be one of {0}".format(sorted(FACT_TYPES)))
    if not isinstance(statement, str) or not statement.strip() or len(statement) > MAX_STATEMENT_CHARS:
        raise EvidenceFactError(
            "statement must be a 1..{0} character string".format(MAX_STATEMENT_CHARS))
    # Re-verify the excerpt against the vault before binding: a tampered or
    # stale selector must never produce a fact.
    excerpt = verify_excerpt_selection(config, selector)

    record: Dict[str, Any] = {
        "fact_type": fact_type,
        "statement": statement,
        "document_id": excerpt["document_id"],
        "document_sha256": excerpt["document_sha256"],
        "parser_version": excerpt["extractor_version"],
        "source_text_sha256": excerpt["source_text_sha256"],
        "excerpt_id": excerpt["excerpt_id"],
        "excerpt_sha256": excerpt["excerpt_sha256"],
        "char_range": {"start": excerpt["start"], "end": excerpt["end"]},
        "computation": None,
    }
    if fact_type == "amount":
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceFactError("amount facts require a numeric value; missing stays absent")
        if unit not in AMOUNT_UNITS:
            raise EvidenceFactError("amount facts require unit in {0}".format(sorted(AMOUNT_UNITS)))
        record["value"] = float(value)
        record["unit"] = unit
    elif fact_type in {"ratio", "count"}:
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EvidenceFactError("{0} value must be numeric".format(fact_type))
            record["value"] = float(value)
        if unit is not None:
            raise EvidenceFactError("{0} facts do not carry amount units".format(fact_type))
    elif value is not None or unit is not None:
        raise EvidenceFactError("{0} facts carry text values only".format(fact_type))
    if computed is not None:
        record["computation"] = _validate_computation(computed)

    return {
        **record,
        "fact_id": "fact:sha256:" + _digest(record),
        "review_status": "unreviewed",
        "scope": "local_review_only",
        "external_eligible": False,
    }


def convert_amount_to_cny(value: float, from_unit: str) -> float:
    """Deterministic CNY normalization for amount facts; the model never converts."""
    if from_unit not in UNIT_TO_CNY_FACTOR:
        raise EvidenceFactError("conversion supports CNY units only: {0}".format(
            sorted(UNIT_TO_CNY_FACTOR)))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceFactError("value must be numeric")
    return float(value) * UNIT_TO_CNY_FACTOR[from_unit]
