"""R05: deterministic fact calculators — whitelisted operations, Decimal math.

The model never computes: every derived numeric fact is recomputed here from
its input facts' ACTUAL stored values, in Decimal with an explicit rounding
rule. The whitelist replaces free-form formula strings (a formula string can
be recorded as prose, but it is never EXECUTED and never earns the
"deterministic" label — GLM_REFACTOR_TASKBOOK R05 stop condition).

Hard rules (A26–A29):

- Inputs are resolved by fact id by the CALLER (facts_service); this module
  receives concrete fact records and never trusts ids on their own.
- Missing value is an ERROR, never zero (A28).
- NaN/Infinity — float or decimal-string — are rejected at the door (A28).
- Division by zero is rejected, never infinity (A28).
- Inputs with DIFFERENT periods block comparable operations unless the caller
  explicitly passes ``allow_mixed_periods=True``, which is recorded in the
  receipt (A29: block, or an explicitly allowed and auditable rule).
- Unit conversions use exact power-of-ten Decimal factors (A27); the period
  and original unit stay in the receipt so the 口径 is preserved.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN
from typing import Any, Dict, List, Optional

from .event_contract import UNIT_TO_CNY_FACTOR

ALLOWED_OPERATIONS = {"sum", "difference", "ratio", "yoy_growth", "convert_unit"}
# Operations whose inputs must share one unit and one period basis.
_COMPARABLE_OPERATIONS = {"sum", "difference", "ratio", "yoy_growth"}
_ROUNDING_MODES = {"ROUND_HALF_UP": ROUND_HALF_UP,
                   "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
                   "ROUND_DOWN": ROUND_DOWN}
DEFAULT_ROUNDING_MODE = "ROUND_HALF_UP"
DEFAULT_ROUNDING_PLACES = 6


class FactCalculationError(ValueError):
    pass


def _input_value(fact: Dict[str, Any], index: int) -> Decimal:
    value = fact.get("value")
    if value is None:
        raise FactCalculationError(
            "input fact #{0} ({1}) has no value; missing is not zero".format(
                index, fact.get("fact_id") or "?"))
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise FactCalculationError(
            "input fact #{0} value must be numeric, got {1}".format(index, type(value).__name__))
    if isinstance(value, float) and not math.isfinite(value):
        raise FactCalculationError(
            "input fact #{0} value is NaN/Infinity; rejected".format(index))
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise FactCalculationError(
            "input fact #{0} value is not a decimal number".format(index)) from exc
    if not number.is_finite():
        raise FactCalculationError(
            "input fact #{0} value is NaN/Infinity; rejected".format(index))
    return number


def _periods(inputs: List[Dict[str, Any]]) -> List[str]:
    return sorted({str(fact.get("period") or "") for fact in inputs})


def _require_same_unit(inputs: List[Dict[str, Any]], operation: str) -> str:
    units = {str(fact.get("unit") or "") for fact in inputs}
    if len(units) != 1 or not next(iter(units), ""):
        raise FactCalculationError(
            "{0} requires all inputs to share one unit; got {1}".format(
                operation, sorted(units)))
    return next(iter(units))


def _round(value: Decimal, places: int, mode: str) -> Decimal:
    if mode not in _ROUNDING_MODES:
        raise FactCalculationError(
            "rounding mode must be one of {0}".format(sorted(_ROUNDING_MODES)))
    if isinstance(places, bool) or int(places) < 0 or int(places) > 18:
        raise FactCalculationError("rounding places must be 0..18")
    quantum = Decimal(1).scaleb(-int(places))
    return value.quantize(quantum, rounding=_ROUNDING_MODES[mode])


def execute_operation(operation: str, *, inputs: List[Dict[str, Any]],
                      params: Optional[Dict[str, Any]] = None,
                      allow_mixed_periods: bool = False,
                      rounding_places: int = DEFAULT_ROUNDING_PLACES,
                      rounding_mode: str = DEFAULT_ROUNDING_MODE) -> Dict[str, Any]:
    """Recompute one whitelisted operation over concrete input facts.

    Returns a VERIFIED RECEIPT (values, units, periods, rounding, result) —
    the counterpart to a fact's computation CLAIM (facts_service separates
    the two; a claim alone never counts as a deterministic calculation).
    """
    if operation not in ALLOWED_OPERATIONS:
        raise FactCalculationError(
            "operation must be one of {0}; free-form formulas are never executed".format(
                sorted(ALLOWED_OPERATIONS)))
    if not isinstance(inputs, list) or not inputs:
        raise FactCalculationError("inputs must be a non-empty list of fact records")
    params = dict(params or {})
    unexpected = set(params) - {"to_unit", "note"}
    if unexpected:
        raise FactCalculationError(
            "unexpected params: {0}".format(sorted(unexpected)))

    periods = _periods(inputs)
    comparable = operation in _COMPARABLE_OPERATIONS
    if comparable and len(periods) > 1 and not allow_mixed_periods:
        raise FactCalculationError(
            "inputs span periods {0}; mixing periods is blocked unless "
            "allow_mixed_periods=True is explicitly set (and gets recorded)".format(periods))

    values = [_input_value(fact, index) for index, fact in enumerate(inputs)]

    if operation == "convert_unit":
        if len(inputs) != 1:
            raise FactCalculationError("convert_unit takes exactly one input fact")
        to_unit = str(params.get("to_unit") or "")
        if to_unit not in UNIT_TO_CNY_FACTOR:
            raise FactCalculationError(
                "convert_unit params.to_unit must be one of {0}".format(
                    sorted(UNIT_TO_CNY_FACTOR)))
        from_unit = str(inputs[0].get("unit") or "")
        if from_unit not in UNIT_TO_CNY_FACTOR:
            raise FactCalculationError(
                "convert_unit input unit {0!r} has no CNY factor".format(from_unit))
        result = values[0] * (Decimal(str(UNIT_TO_CNY_FACTOR[from_unit]))
                              / Decimal(str(UNIT_TO_CNY_FACTOR[to_unit])))
        result_unit = to_unit
    elif operation == "sum":
        result_unit = _require_same_unit(inputs, "sum")
        result = sum(values, Decimal(0))
    elif operation == "difference":
        if len(inputs) != 2:
            raise FactCalculationError("difference takes exactly two input facts (a - b)")
        result_unit = _require_same_unit(inputs, "difference")
        result = values[0] - values[1]
    elif operation == "ratio":
        if len(inputs) != 2:
            raise FactCalculationError("ratio takes exactly two input facts (a / b)")
        _require_same_unit(inputs, "ratio")
        if values[1] == 0:
            raise FactCalculationError(
                "ratio denominator is zero; division by zero is rejected, not infinity")
        result = values[0] / values[1]
        result_unit = "ratio"
    else:  # yoy_growth: (a - b) / b, same unit, growth factor
        if len(inputs) != 2:
            raise FactCalculationError("yoy_growth takes exactly two input facts (current, prior)")
        _require_same_unit(inputs, "yoy_growth")
        if values[1] == 0:
            raise FactCalculationError(
                "yoy_growth prior-period base is zero; division by zero is rejected")
        result = (values[0] - values[1]) / values[1]
        result_unit = "growth_factor"

    rounded = _round(result, rounding_places, rounding_mode)
    return {
        "operation": operation,
        "inputs": [
            {"fact_id": str(fact.get("fact_id") or ""),
             "value": fact.get("value"),
             "unit": str(fact.get("unit") or ""),
             "period": str(fact.get("period") or "")}
            for fact in inputs
        ],
        "params": params,
        "result": {"value": float(rounded), "decimal_text": str(rounded),
                   "unit": result_unit},
        "rounding": {"mode": rounding_mode, "places": int(rounding_places)},
        "computed_with": "decimal",
        "allow_mixed_periods": bool(allow_mixed_periods),
        "input_periods": periods,
    }
