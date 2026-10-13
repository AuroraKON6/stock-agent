"""Kimi integration contracts: typed snapshot loader and request validation.

F03 remediation. The external packages parse markdown contracts at runtime and
collapse object/array types to strings; this module instead loads a frozen,
reviewed JSON snapshot (see config/candidates/kimi/contracts.v1.json) and
validates every query parameter against it before anything is sent. Unknown
APIs, unknown parameter names, enum violations, oversized windows and caller
supplied file_path values all fail closed. Importing this module has no side
effects: no file reads, no network, no subprocesses.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = "1.0"

PARAM_TYPES = {
    "a_share_ticker_list", "date", "enum", "statement_selector",
    "report_period", "search_query", "virtual_artifact_name",
}
API_FIELDS = {
    "api_id", "source_id", "data_source_name", "api_name", "transport",
    "data_kind", "purpose", "params", "constraints", "result_contract",
    "known_unknowns", "notes",
}
KNOWN_DATA_KINDS = {
    "daily_quote", "announcement_metadata", "financial_statement",
    "news_metadata", "provider_analysis",
}
# A-share codes only for the first batch; the upstream contract also lists HK/US
# formats, which stay out of scope until a reviewed contract includes them.
TICKER_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
# Windows drive-relative, UNC, ADS and parent-escape shapes that must never
# influence a local path decision; checked on caller strings and remote names.
_LOCAL_PATH_SHAPE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\|^//|^[\\/]|[\\/]\.\.[\\/]|:\S")
_STATEMENT_ALIASES = {
    "bs": "balance_sheet", "is": "income_statement", "cf": "cash_flow",
    "balance_sheet": "balance_sheet", "income_statement": "income_statement",
    "cash_flow": "cash_flow", "all": "all",
}
_REPORT_PERIOD_SUFFIXES = {"0331", "0630", "0930", "1231"}
_CREDENTIAL_SHAPE = re.compile(r"api[_-]?key|authorization:|bearer |sk-[A-Za-z0-9]{16,}", re.I)


class KimiContractError(ValueError):
    """Fail-closed validation error; message must stay secret-free."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_contract_snapshot(path: Path) -> Dict[str, Any]:
    """Load and structurally validate a contract snapshot; hash is computed
    over the canonical form so request records can pin the exact contract."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KimiContractError("contract snapshot unreadable: {0}".format(exc)) from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise KimiContractError("contract snapshot schema_version must be {0!r}".format(SCHEMA_VERSION))
    apis = document.get("apis")
    if not isinstance(apis, list) or not apis:
        raise KimiContractError("contract snapshot must list at least one api")
    seen: set = set()
    for api in apis:
        if not isinstance(api, dict) or set(api) != API_FIELDS:
            raise KimiContractError(
                "api entry must have exactly {0}".format(sorted(API_FIELDS)))
        api_id = api["api_id"]
        if not isinstance(api_id, str) or not api_id or api_id in seen:
            raise KimiContractError("api_id must be a non-empty unique string")
        seen.add(api_id)
        for field in ("source_id", "data_source_name", "api_name", "transport", "purpose"):
            if not isinstance(api[field], str) or not api[field].strip():
                raise KimiContractError("api {0}: field {1} must be a non-empty string".format(api_id, field))
        if api["data_kind"] not in KNOWN_DATA_KINDS:
            raise KimiContractError(
                "api {0}: data_kind must be one of {1}".format(api_id, sorted(KNOWN_DATA_KINDS)))
        _validate_params(api_id, api["params"])
        if not isinstance(api["constraints"], dict):
            raise KimiContractError("api {0}: constraints must be an object".format(api_id))
    document = dict(document)
    document["contract_hash"] = hashlib.sha256(
        _canonical_json({"apis": apis, "schema_version": SCHEMA_VERSION}).encode("utf-8")
    ).hexdigest()
    return document


def _validate_params(api_id: str, params: Any) -> None:
    if not isinstance(params, dict) or not params:
        raise KimiContractError("api {0}: params must be a non-empty object".format(api_id))
    for name, spec in params.items():
        if not isinstance(name, str) or not name:
            raise KimiContractError("api {0}: parameter names must be strings".format(api_id))
        if not isinstance(spec, dict):
            raise KimiContractError("api {0}: param {1} spec must be an object".format(api_id, name))
        ptype = spec.get("type")
        if ptype not in PARAM_TYPES:
            raise KimiContractError(
                "api {0}: param {1} has unknown type {2!r}".format(api_id, name, ptype))
        if not isinstance(spec.get("required"), bool):
            raise KimiContractError("api {0}: param {1} requires a boolean".format(api_id, name))
        if ptype == "enum":
            options = spec.get("options")
            if not isinstance(options, list) or not options:
                raise KimiContractError("api {0}: enum param {1} needs options".format(api_id, name))


def find_api(snapshot: Dict[str, Any], api_id: str) -> Dict[str, Any]:
    for api in snapshot["apis"]:
        if api["api_id"] == api_id:
            return api
    raise KimiContractError(
        "unknown api_id {0!r}: not in frozen contract snapshot (fail closed)".format(api_id))


def _require_string(api_id: str, name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise KimiContractError("api {0}: param {1} must be a string".format(api_id, name))
    return value


def _parse_date(api_id: str, name: str, value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise KimiContractError(
            "api {0}: param {1} must be YYYY-MM-DD".format(api_id, name)) from exc
    if parsed.isoformat() != value:
        raise KimiContractError(
            "api {0}: param {1} must be strict YYYY-MM-DD".format(api_id, name))
    return parsed


def check_local_path_shape(api_id: str, name: str, value: str) -> None:
    if _LOCAL_PATH_SHAPE.search(value):
        raise KimiContractError(
            "api {0}: param {1} looks like a local path/UNC/ADS reference; "
            "local paths are never sent to the gateway".format(api_id, name))


def normalize_params(api: Dict[str, Any], raw_params: Dict[str, Any], *, poc: bool = True) -> Dict[str, Any]:
    """Validate caller parameters against the frozen contract.

    Returns the normalized parameter dict that the transport adapter will
    serialize. file_path is host generated and always rejected from callers.
    With poc=True the stricter proof-of-concept constraints apply (1 ticker,
    short windows, explicit adjustment basis, single statement).
    """
    api_id = api["api_id"]
    if not isinstance(raw_params, dict):
        raise KimiContractError("api {0}: params must be an object".format(api_id))
    contract_params: Dict[str, Dict[str, Any]] = api["params"]
    unknown = set(raw_params) - set(contract_params)
    if unknown:
        raise KimiContractError(
            "api {0}: unknown parameters {1}; contract snapshot is exhaustive".format(
                api_id, sorted(unknown)))
    normalized: Dict[str, Any] = {}
    for name, spec in contract_params.items():
        if name not in raw_params:
            # virtual_artifact_name is host generated: the upstream contract
            # may mark it required, but callers never supply it and the
            # adapter always synthesizes a virtual name at send time.
            if spec["required"] and spec["type"] != "virtual_artifact_name":
                raise KimiContractError("api {0}: missing required param {1}".format(api_id, name))
            if "default" in spec:
                normalized[name] = spec["default"]
            continue
        value = raw_params[name]
        ptype = spec["type"]
        if ptype == "virtual_artifact_name":
            raise KimiContractError(
                "api {0}: param {1} is host generated; callers may not supply it".format(api_id, name))
        normalized[name] = _check_typed(api_id, name, spec, value)
    _check_constraints(api, normalized, poc=poc)
    return normalized


def _check_typed(api_id: str, name: str, spec: Dict[str, Any], value: Any) -> Any:
    ptype = spec["type"]
    value = _require_string(api_id, name, value)
    if ptype == "a_share_ticker_list":
        items = [item.strip() for item in value.split(",") if item.strip()]
        if not items:
            raise KimiContractError("api {0}: param {1} must list at least one ticker".format(api_id, name))
        if len(items) > int(spec.get("max_items", 3)):
            raise KimiContractError(
                "api {0}: param {1} exceeds max {2} tickers".format(api_id, name, spec.get("max_items", 3)))
        for item in items:
            if not TICKER_PATTERN.match(item):
                raise KimiContractError(
                    "api {0}: ticker {1!r} is not a first-batch A-share code (XXXXXX.SH/SZ/BJ)".format(api_id, item))
        return ",".join(items)
    if ptype == "date":
        _parse_date(api_id, name, value)
        return value
    if ptype == "enum":
        if value not in spec["options"]:
            raise KimiContractError(
                "api {0}: param {1} must be one of {2}".format(api_id, name, spec["options"]))
        return value
    if ptype == "statement_selector":
        parts = [part.strip().lower() for part in value.split(",") if part.strip()]
        if not parts:
            raise KimiContractError("api {0}: param {1} must select a statement".format(api_id, name))
        resolved = []
        for part in parts:
            if part not in _STATEMENT_ALIASES:
                raise KimiContractError(
                    "api {0}: statement {1!r} unknown; use all/balance_sheet/income_statement/cash_flow".format(api_id, part))
            resolved.append(_STATEMENT_ALIASES[part])
        if "all" in resolved and len(resolved) > 1:
            raise KimiContractError("api {0}: 'all' cannot combine with other statements".format(api_id))
        return ",".join(resolved)
    if ptype == "report_period":
        if len(value) != 8 or not value.isdigit() or value[4:] not in _REPORT_PERIOD_SUFFIXES:
            raise KimiContractError(
                "api {0}: param {1} must be YYYY + 0331/0630/0930/1231".format(api_id, name))
        year = int(value[:4])
        if not 1990 <= year <= 2100:
            raise KimiContractError("api {0}: report year {1} out of range".format(api_id, year))
        return value
    if ptype == "search_query":
        if not value.strip():
            raise KimiContractError("api {0}: param {1} must not be empty".format(api_id, name))
        max_length = int(spec.get("max_length", 200))
        if len(value) > max_length:
            raise KimiContractError(
                "api {0}: param {1} exceeds {2} chars (minimize the outbound query)".format(api_id, name, max_length))
        check_local_path_shape(api_id, name, value)
        if _CREDENTIAL_SHAPE.search(value):
            raise KimiContractError(
                "api {0}: param {1} looks like a credential; never egress secrets".format(api_id, name))
        return value.strip()
    raise KimiContractError("api {0}: unhandled param type {1}".format(api_id, ptype))


def _check_constraints(api: Dict[str, Any], normalized: Dict[str, Any], *, poc: bool) -> None:
    api_id = api["api_id"]
    constraints = api.get("constraints") or {}
    start = normalized.get("start_date")
    end = normalized.get("end_date")
    if start is not None and end is not None:
        start_date = _parse_date(api_id, "start_date", start)
        end_date = _parse_date(api_id, "end_date", end)
        if end_date < start_date:
            raise KimiContractError("api {0}: end_date before start_date".format(api_id))
        window = (end_date - start_date).days
        max_window = int(constraints.get("max_window_days") or 0)
        if max_window and window > max_window:
            raise KimiContractError(
                "api {0}: window {1} days exceeds contract max {2}".format(api_id, window, max_window))
        if poc:
            poc_window = int((constraints.get("poc") or {}).get("max_window_days") or max_window or 0)
            if poc_window and window > poc_window:
                raise KimiContractError(
                    "api {0}: window {1} days exceeds PoC max {2}".format(api_id, window, poc_window))
    if poc:
        poc_rules = constraints.get("poc") or {}
        max_tickers = poc_rules.get("max_tickers")
        ticker = normalized.get("ticker")
        if max_tickers and ticker and len([t for t in ticker.split(",") if t]) > int(max_tickers):
            raise KimiContractError(
                "api {0}: PoC allows at most {1} ticker(s)".format(api_id, max_tickers))
        if poc_rules.get("adjust_must_be_explicit") and "adjust" not in normalized:
            raise KimiContractError(
                "api {0}: adjustment basis must be explicit (forward/backward/none); "
                "market defaults are never recorded implicitly".format(api_id))
        if poc_rules.get("interval_must_be") and normalized.get("interval") != poc_rules["interval_must_be"]:
            raise KimiContractError(
                "api {0}: PoC requires interval {1!r}".format(api_id, poc_rules["interval_must_be"]))
        max_statements = poc_rules.get("max_statements")
        statement = normalized.get("statement")
        if max_statements and statement:
            selected = statement.split(",")
            if len(selected) > int(max_statements) or "all" in selected:
                raise KimiContractError(
                    "api {0}: PoC allows at most {1} explicit statement(s), 'all' excluded".format(api_id, max_statements))
        if poc_rules.get("max_queries_per_event"):
            # enforced by the client-level attempt budget, recorded here for the contract record
            pass


def virtual_artifact_name(api: Dict[str, Any], artifact_ext: str = "csv") -> str:
    """Host-generated virtual name sent to the gateway for its required
    file_path parameter. Deterministically unique per call (uuid4), never
    reflects a real local path, and is not used for local file naming."""
    import uuid
    safe_ext = artifact_ext.strip(".").lower() if artifact_ext.strip(".") else "csv"
    if not re.match(r"^[a-z0-9]{1,5}$", safe_ext):
        safe_ext = "csv"
    return "kimi_{0}_{1}.{2}".format(api["api_name"], uuid.uuid4().hex[:12], safe_ext)


def idempotency_key(snapshot: Dict[str, Any], api: Dict[str, Any], normalized: Dict[str, Any]) -> str:
    """Stable key over source, api, contract hash, normalized params — not
    just the ticker (plan section 7.1)."""
    material = {
        "data_source_name": api["data_source_name"],
        "api_name": api["api_name"],
        "contract_hash": snapshot["contract_hash"],
        "params": {name: normalized[name] for name in sorted(normalized)},
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
