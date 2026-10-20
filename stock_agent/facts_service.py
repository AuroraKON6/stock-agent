"""R05: facts service — separate computation CLAIMS from VERIFIED receipts.

``evidence_facts.build_fact`` records what a fact CLAIMS: a value plus (for
derived facts) a computation declaration. A claim is data. It only becomes a
"verified deterministic calculation" when this service re-resolves the
declared input facts by id, recomputes the whitelisted operation in Decimal
(:mod:`.fact_calculators`), and the recomputation matches the claimed value
and unit. The receipt carries the actual inputs (values/units/periods), the
rounding rule and the result — so a downstream reader can always distinguish
"声明" from "已验证回执" and re-audit the chain.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from .fact_calculators import FactCalculationError, execute_operation

# Claimed vs recomputed comparison tolerance: values are stored as floats, so
# an exact decimal compare of the SHORTEST float repr is the rule; this tiny
# relative tolerance only absorbs float-storage artifacts, never real drift.
_REL_TOLERANCE = Decimal("1e-12")


class FactVerificationError(ValueError):
    pass


def _claim(computation: Any) -> Dict[str, Any]:
    if not isinstance(computation, dict):
        raise FactVerificationError("computation claim must be an object")
    operation = computation.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        raise FactVerificationError(
            "computation claim must name a whitelisted operation; "
            "free-form formula strings are declarations, never executions")
    input_ids = computation.get("input_fact_ids")
    if not isinstance(input_ids, list) or not input_ids or \
            not all(isinstance(item, str) and item.strip() for item in input_ids):
        raise FactVerificationError(
            "computation claim must reference at least one input fact id")
    extra = set(computation) - {"operation", "input_fact_ids", "params", "converted_by"}
    if extra:
        raise FactVerificationError(
            "unexpected computation fields: {0}".format(sorted(extra)))
    return {"operation": operation, "input_fact_ids": list(input_ids),
            "params": dict(computation.get("params") or {})}


def _values_match(claimed: Any, recomputed: Decimal) -> bool:
    try:
        left = Decimal(str(claimed))
        right = Decimal(str(recomputed))
    except Exception:
        return False
    if left == right:
        return True
    if right == 0:
        return left == 0
    difference = abs(left - right)
    tolerance = abs(right) * _REL_TOLERANCE
    return difference <= tolerance


def verify_computed_fact(fact: Dict[str, Any],
                         input_facts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve a fact's computation claim against concrete inputs and recompute.

    ``input_facts`` maps fact_id -> fact record for every id the claim names;
    ids NOT present there are unknown (A26: a fabricated id refuses the
    verification — it can never quietly become a missing-number 0).
    Returns a verdict dict; ``attach_verified_receipt`` turns a positive
    verdict into the stored "已验证计算回执".
    """
    computation = fact.get("computation")
    if computation is None:
        return {"verified": False, "reason": "no_computation_claim",
                "detail": "only derived facts with a computation claim can be verified"}
    claim = _claim(computation)
    own_id = str(fact.get("fact_id") or "")
    resolved: List[Dict[str, Any]] = []
    for fact_id in claim["input_fact_ids"]:
        if fact_id == own_id:
            return {"verified": False, "reason": "self_referential_input",
                    "detail": "a computation cannot use its own fact as input"}
        record = input_facts.get(fact_id)
        if record is None:
            return {"verified": False, "reason": "unknown_input_fact",
                    "detail": "input fact {0} was not provided; fabricated ids refuse "
                              "verification instead of degrading to zero".format(fact_id)}
        if str(record.get("fact_id") or "") != fact_id:
            return {"verified": False, "reason": "input_id_mismatch",
                    "detail": "record provided for {0} carries a different fact_id".format(fact_id)}
        resolved.append(record)
    claimed_value = fact.get("value")
    if claimed_value is None:
        return {"verified": False, "reason": "claimed_value_missing",
                "detail": "the derived fact declares no value to verify against"}
    try:
        receipt = execute_operation(
            claim["operation"], inputs=resolved, params=claim["params"])
    except FactCalculationError as exc:
        return {"verified": False, "reason": "recomputation_rejected",
                "detail": str(exc)}
    if not _values_match(claimed_value, Decimal(receipt["result"]["decimal_text"])):
        return {"verified": False, "reason": "value_mismatch",
                "claimed": claimed_value,
                "recomputed": receipt["result"]["value"],
                "detail": "claimed value does not match the Decimal recomputation"}
    claimed_unit = str(fact.get("unit") or "")
    if claimed_unit and claimed_unit != str(receipt["result"]["unit"]):
        return {"verified": False, "reason": "unit_mismatch",
                "claimed_unit": claimed_unit,
                "recomputed_unit": receipt["result"]["unit"],
                "detail": "claimed unit differs from the recomputed result unit"}
    return {"verified": True, "reason": "ok", "receipt": receipt}


def attach_verified_receipt(fact: Dict[str, Any], verdict: Dict[str, Any]) -> Dict[str, Any]:
    """Store a POSITIVE verdict on a copy of the fact as the verified receipt.

    A failed verification attaches nothing: the fact keeps its claim and stays
    unverified — falsifying a receipt is not an option (R05: 声明与回执分开).
    """
    if not verdict.get("verified"):
        raise FactVerificationError(
            "only a positive verification produces a receipt; reason={0}".format(
                verdict.get("reason")))
    updated = dict(fact)
    updated["verified_computation"] = verdict["receipt"]
    updated["computation_verified"] = True
    return updated
