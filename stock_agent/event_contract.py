"""GLM-02 event timing and numeric-unit contract.

Keeps source_time / observed_at / fetched_at separate and explicit:
missing times stay missing (never zero- or fetch-filled), future times are
flagged and preserved, and ordering violations are reported. Price/amount
observations must declare unit and adjustment explicitly; missing values
remain None so nothing downstream can mistake them for zero.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

# Tolerated clock skew between local fetch time and upstream timestamps.
_FUTURE_TOLERANCE_SECONDS = 300

AMOUNT_UNITS = {"cny", "cny_wan", "cny_yi"}
UNIT_TO_CNY_FACTOR = {"cny": 1.0, "cny_wan": 10_000.0, "cny_yi": 100_000_000.0}
PRICE_ADJUSTMENTS = {"none", "forward", "backward"}


def _parse_instant(value: Any, *, default_tz: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not default_tz:
            return None
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(default_tz))
        except Exception:
            return None
    return parsed


def _status_for(value: Any, parsed: Optional[datetime]) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "missing"
    return "valid" if parsed is not None else "unparseable"


def build_event_timing(
    *,
    source_time: Optional[str],
    observed_at: Optional[str],
    fetched_at: str,
    default_tz: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise ValueError("fetched_at is required so timing can never be implicit")
    fetched_parsed = _parse_instant(fetched_at, default_tz=default_tz)
    if fetched_parsed is None:
        raise ValueError("fetched_at must be a parseable ISO timestamp")

    source_parsed = _parse_instant(source_time, default_tz=default_tz)
    observed_parsed = _parse_instant(observed_at, default_tz=default_tz)

    source_status = _status_for(source_time, source_parsed)
    observed_status = _status_for(observed_at, observed_parsed)
    assumed_tz_source = bool(
        source_time and source_parsed
        and isinstance(source_time, str)
        and datetime.fromisoformat(source_time.strip()).tzinfo is None
        and default_tz
    )

    violation = ""
    if source_parsed and source_parsed > fetched_parsed.astimezone(source_parsed.tzinfo):
        delta = (source_parsed - fetched_parsed.astimezone(source_parsed.tzinfo)).total_seconds()
        if delta > _FUTURE_TOLERANCE_SECONDS:
            source_status = "future"
            violation = "source_after_fetch"
    if source_parsed and observed_parsed and observed_parsed < source_parsed:
        violation = violation or "observed_before_source"
    if observed_parsed and observed_parsed > fetched_parsed.astimezone(observed_parsed.tzinfo):
        delta = (observed_parsed - fetched_parsed.astimezone(observed_parsed.tzinfo)).total_seconds()
        if delta > _FUTURE_TOLERANCE_SECONDS:
            observed_status = "future"
            violation = violation or "observed_after_fetch"

    def _display(raw: Any, parsed: Optional[datetime]) -> Any:
        # Normalized instant when interpretable; the as-received value otherwise
        # (unparseable claims stay visible for audit, missing stays None).
        if parsed is not None:
            return parsed.isoformat()
        return raw if raw is not None else None

    return {
        "source_time": _display(source_time, source_parsed),
        "observed_at": _display(observed_at, observed_parsed),
        "source_time_raw": source_time if source_time is not None else None,
        "observed_at_raw": observed_at if observed_at is not None else None,
        "fetched_at": fetched_at,
        "source_time_status": source_status,
        "observed_at_status": observed_status,
        "fetched_at_status": "valid",
        "source_time_tz": "assumed" if assumed_tz_source else "declared",
        "ordering_valid": not violation,
        "ordering_violation": violation,
    }


def build_price_observation(
    *,
    symbol: str,
    value: Optional[float],
    unit: str,
    adjusted: Optional[str],
    observed_at: str,
    source: str,
    fetched_at: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol is required")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if unit not in AMOUNT_UNITS:
        raise ValueError("unit must be one of {0}".format(sorted(AMOUNT_UNITS)))
    if adjusted not in PRICE_ADJUSTMENTS:
        raise ValueError("adjusted must explicitly be one of {0}".format(sorted(PRICE_ADJUSTMENTS)))

    observation: Dict[str, Any] = {
        "symbol": symbol,
        "value": value,
        "unit": unit,
        "currency": "cny",
        "adjusted": adjusted,
        "observed_at": observed_at,
        "source": source,
    }
    if value is None:
        observation["status"] = "missing"
        return observation
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be numeric or None, never a placeholder string")
    observation["value"] = float(value)

    status = "valid"
    parsed = _parse_instant(observed_at, default_tz=None)
    if parsed is None:
        status = "unparseable_observation_time"
    elif fetched_at:
        fetched_parsed = _parse_instant(fetched_at, default_tz=None)
        if fetched_parsed and (parsed - fetched_parsed).total_seconds() > _FUTURE_TOLERANCE_SECONDS:
            status = "future_observation"
    observation["status"] = status
    return observation


def convert_cny_amount(value: float, from_unit: str, to_unit: str = "cny") -> float:
    """Deterministic unit conversion for CNY amounts; the model never converts."""
    if from_unit not in UNIT_TO_CNY_FACTOR or to_unit not in UNIT_TO_CNY_FACTOR:
        raise ValueError("conversion supports CNY units only: {0}".format(sorted(UNIT_TO_CNY_FACTOR)))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be numeric")
    return float(value) * UNIT_TO_CNY_FACTOR[from_unit] / UNIT_TO_CNY_FACTOR[to_unit]


def event_identity(*, source_id: str, url: str, title: str,
                   content_version: str = "1") -> str:
    """Stable event identity; revisions must bump content_version, not rewrite history."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required for event identity")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required for event identity")
    digest = hashlib.sha256(json.dumps(
        {"source_id": source_id, "url": url, "title": title,
         "content_version": str(content_version)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return "event:sha256:{0}".format(digest)


def event_base_identity(*, source_id: str, url: str, title: str) -> str:
    """Identity of the event itself across content revisions (supersedes chain anchor)."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required for event identity")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required for event identity")
    digest = hashlib.sha256(json.dumps(
        {"source_id": source_id, "url": url, "title": title},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return "event-base:sha256:{0}".format(digest)
