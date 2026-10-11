from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..config import project_path
from ..models import RawSignal, now_iso
from .example_data import looks_like_example_path, mark_payload_example


def collect_filing_jsonl(source_configs: Iterable[Dict[str, Any]]) -> List[RawSignal]:
    signals: List[RawSignal] = []
    for source_config in source_configs:
        if not source_config.get("enabled", True):
            continue
        path = project_path(str(source_config.get("path", "")))
        if not path.exists():
            continue
        signals.extend(_collect_jsonl_path(path, source_config))
    return signals


def _collect_jsonl_path(path: Path, source_config: Dict[str, Any]) -> List[RawSignal]:
    """Collect signals from a single JSONL file or from a landing directory.

    A directory is scanned for regular direct-child ``*.jsonl`` files in
    deterministic path-name order. Subdirectories and non-JSONL files are
    ignored, and an empty directory yields no signals and no error. A bare
    file keeps the original single-file behavior.
    """
    if path.is_dir():
        signals: List[RawSignal] = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            if child.is_file() and child.suffix == ".jsonl":
                signals.extend(_read_jsonl(child, source_config))
        return signals
    return _read_jsonl(path, source_config)


def _read_jsonl(path: Path, source_config: Dict[str, Any]) -> List[RawSignal]:
    is_example = looks_like_example_path(path)
    signals: List[RawSignal] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid filing JSONL at {0}:{1}: {2}".format(path, line_number, exc)) from exc
        signal = _row_to_signal(row, source_config)
        if is_example:
            mark_payload_example(signal.payload)
        if signal.title:
            signals.append(signal)
    return signals


def _row_to_signal(row: Dict[str, Any], source_config: Dict[str, Any]) -> RawSignal:
    provider = str(source_config.get("name") or row.get("provider") or row.get("source") or "filing_jsonl")
    source = str(row.get("source") or "filing_jsonl")
    source_type = str(row.get("source_type") or "filing")
    title = str(row.get("title") or "").strip()
    company = str(row.get("company") or row.get("name") or "").strip()
    symbol = str(row.get("symbol") or row.get("code") or "").strip()
    announcement_type = str(row.get("announcement_type") or row.get("filing_type") or "").strip()
    report_period = str(row.get("report_period") or row.get("period") or "").strip()
    url = str(row.get("url") or row.get("source_url") or "").strip()
    published_at = str(row.get("published_at") or row.get("announcement_date") or now_iso())
    summary = str(row.get("summary") or row.get("content_summary") or "").strip()
    key_points = _list_of_strings(row.get("key_points", []))
    risk_points = _list_of_strings(row.get("risk_points", []))
    metrics = row.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    if not title:
        title = "{0}{1}".format(company or symbol or "公告", _type_label(announcement_type))

    filing_evidence = _build_filing_evidence(
        provider=provider,
        symbol=symbol,
        company=company,
        title=title,
        announcement_type=announcement_type,
        report_period=report_period,
        published_at=published_at,
        url=url,
        key_points=key_points,
        risk_points=risk_points,
    )
    financial_evidence = _build_financial_evidence(metrics, report_period)

    return RawSignal(
        source=source,
        source_type=source_type,
        title=title,
        summary=summary or _summary_from_evidence(key_points, risk_points, metrics),
        url=url,
        published_at=published_at,
        payload={
            **row,
            "symbol": symbol,
            "company": company,
            "announcement_type": announcement_type,
            "report_period": report_period,
            "key_points": key_points,
            "risk_points": risk_points,
            "metrics": metrics,
            "filing_evidence": filing_evidence,
            "financial_evidence": financial_evidence,
        },
    )


def _build_filing_evidence(
    *,
    provider: str,
    symbol: str,
    company: str,
    title: str,
    announcement_type: str,
    report_period: str,
    published_at: str,
    url: str,
    key_points: List[str],
    risk_points: List[str],
) -> Dict[str, Any]:
    fields = {
        "symbol": symbol,
        "company": company,
        "title": title,
        "announcement_type": announcement_type,
        "report_period": report_period,
        "published_at": published_at,
        "url": url,
    }
    required = ["symbol", "title", "announcement_type", "report_period", "url"]
    present = [key for key, value in fields.items() if value not in ("", None)]
    missing = [key for key in required if fields.get(key) in ("", None)]
    status = "ok" if not missing else "partial"
    return {
        "provider": provider,
        "status": status,
        "symbol": symbol,
        "company": company,
        "title": title,
        "announcement_type": announcement_type,
        "report_period": report_period,
        "published_at": published_at,
        "url": url,
        "key_points": key_points,
        "risk_points": risk_points,
        "fields_present": present,
        "missing_fields": missing,
        "human_review_required": bool(missing),
        "extracted_at": now_iso(),
        "notes": [] if status == "ok" else ["公告/财报关键字段缺失，需人工补证据后再提高置信度。"],
    }


def _build_financial_evidence(metrics: Dict[str, Any], report_period: str) -> Dict[str, Any]:
    clean_metrics = {str(key): value for key, value in metrics.items()}
    return {
        "status": "ok" if clean_metrics else "missing",
        "report_period": report_period,
        "metrics": clean_metrics,
        "metric_count": len(clean_metrics),
        "missing_fields": [] if clean_metrics else ["metrics"],
    }


def _summary_from_evidence(key_points: List[str], risk_points: List[str], metrics: Dict[str, Any]) -> str:
    parts = []
    if key_points:
        parts.append("要点: {0}".format("；".join(key_points[:3])))
    if risk_points:
        parts.append("风险: {0}".format("；".join(risk_points[:3])))
    if metrics:
        metric_pairs = ["{0}={1}".format(key, value) for key, value in list(metrics.items())[:4]]
        parts.append("指标: {0}".format("；".join(metric_pairs)))
    return "；".join(parts)


def _list_of_strings(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


def _type_label(value: str) -> str:
    if not value:
        return ""
    return " {0}".format(value)
