from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..config import project_path
from ..analysis import _score_filing_importance
from ..models import RawSignal, now_iso


EASTMONEY_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"


def collect_eastmoney_filings(source_config: Dict[str, Any]) -> List[RawSignal]:
    if not source_config.get("enabled", False):
        return []

    symbols = [str(item).strip() for item in source_config.get("symbols", []) if str(item).strip()]
    if not symbols:
        return []

    page_size = max(1, int(source_config.get("page_size", 20)))
    max_per_symbol = max(1, int(source_config.get("max_announcements_per_symbol", 5)))
    timeout = int(source_config.get("timeout", 20))
    ann_type = str(source_config.get("ann_type") or "A")

    signals: List[RawSignal] = []
    for symbol in symbols:
        try:
            rows = _fetch_announcements(symbol, page_size=page_size, timeout=timeout, ann_type=ann_type)
        except Exception as exc:
            signals.append(_source_error(symbol, str(exc)))
            continue
        for row in rows[:max_per_symbol]:
            signals.append(_row_to_signal(row, source_config))

    filing_signals = [signal for signal in signals if signal.source_type == "filing"]
    if filing_signals and source_config.get("write_jsonl", True):
        _write_landing_jsonl(
            filing_signals,
            output_dir=str(source_config.get("output_dir") or "data/inbox/filings"),
        )
    return signals


def _fetch_announcements(symbol: str, *, page_size: int, timeout: int, ann_type: str) -> List[Dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "sr": "-1",
            "page_size": str(page_size),
            "page_index": "1",
            "ann_type": ann_type,
            "client_source": "web",
            "stock_list": symbol,
        }
    )
    request = urllib.request.Request(
        EASTMONEY_ANN_URL + "?" + query,
        headers={
            "User-Agent": "Mozilla/5.0 stock-agent/0.1",
            "Referer": "https://data.eastmoney.com/notices/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    rows = data.get("data", {}).get("list", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _row_to_signal(row: Dict[str, Any], source_config: Dict[str, Any]) -> RawSignal:
    code_info = _first_dict(row.get("codes", []))
    code = str(code_info.get("stock_code") or row.get("stock_code") or "").strip()
    company = str(code_info.get("short_name") or row.get("short_name") or code).strip()
    art_code = str(row.get("art_code") or "").strip()
    title = str(row.get("title_ch") or row.get("title") or "").strip()
    columns = [str(item.get("column_name")) for item in row.get("columns", []) if isinstance(item, dict) and item.get("column_name")]
    published_at = _clean_time(row.get("notice_date") or row.get("display_time") or row.get("sort_date") or now_iso())
    announcement_type = _classify_announcement(title, columns)
    report_period = _infer_report_period(title)
    url = str(row.get("url") or "").strip()
    if not url and code and art_code:
        url = "https://data.eastmoney.com/notices/detail/{0}/{1}.html".format(code, art_code)
    key_points = _build_key_points(title, columns)
    risk_points: List[str] = []
    summary = "公告类型={0}; 报告期={1}; 栏目={2}; 公告日期={3}".format(
        announcement_type or "unknown",
        report_period or "unknown",
        "、".join(columns) if columns else "unknown",
        published_at,
    )
    filing_evidence = _build_filing_evidence(
        symbol=code,
        company=company,
        title=title,
        announcement_type=announcement_type,
        report_period=report_period,
        published_at=published_at,
        url=url,
        key_points=key_points,
        risk_points=risk_points,
    )
    signal = RawSignal(
        source="eastmoney_filing",
        source_type=str(source_config.get("source_type") or "filing"),
        title=title,
        summary=summary,
        url=url,
        published_at=published_at,
        payload={
            "symbol": code,
            "company": company,
            "art_code": art_code,
            "announcement_type": announcement_type,
            "report_period": report_period,
            "key_points": key_points,
            "risk_points": risk_points,
            "metrics": {},
            "columns": columns,
            "filing_evidence": filing_evidence,
            "financial_evidence": {
                "status": "missing",
                "report_period": report_period,
                "metrics": {},
                "metric_count": 0,
                "missing_fields": ["metrics"],
            },
            "eastmoney_raw": row,
        },
    )
    _score_filing_importance(signal, {})
    return signal


def _build_filing_evidence(
    *,
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
        "published_at": published_at,
        "url": url,
    }
    required = ["symbol", "title", "announcement_type", "published_at", "url"]
    if _requires_report_period(announcement_type):
        fields["report_period"] = report_period
        required.append("report_period")
    present = [key for key, value in fields.items() if value not in ("", None)]
    missing = [key for key in required if fields.get(key) in ("", None)]
    status = "ok" if not missing else "partial"
    return {
        "provider": "eastmoney_filing",
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
        "notes": [] if status == "ok" else ["公告关键字段缺失，需人工核验原始公告。"],
    }


def _to_filing_jsonl_record(signal: RawSignal) -> Dict[str, Any]:
    payload = signal.payload or {}
    filing = payload.get("filing_evidence", {})
    financial = payload.get("financial_evidence", {})
    return {
        "source": "eastmoney_filing",
        "source_type": "filing",
        "symbol": filing.get("symbol") or payload.get("symbol", ""),
        "company": filing.get("company") or payload.get("company", ""),
        "title": signal.title,
        "announcement_type": filing.get("announcement_type") or payload.get("announcement_type", ""),
        "report_period": filing.get("report_period") or payload.get("report_period", ""),
        "published_at": signal.published_at,
        "url": signal.url,
        "summary": signal.summary,
        "key_points": filing.get("key_points") or payload.get("key_points", []),
        "risk_points": filing.get("risk_points") or payload.get("risk_points", []),
        "metrics": financial.get("metrics", {}),
        "filing_importance_level": payload.get("filing_importance_level") or filing.get("importance_level", "unknown"),
        "filing_importance_score": payload.get("filing_importance_score") or filing.get("importance_score", 0),
        "filing_importance_reasons": payload.get("filing_importance_reasons") or filing.get("importance_reasons", []),
        "filing_evidence": filing,
        "financial_evidence": financial,
        "collected_at": now_iso(),
    }


def _write_landing_jsonl(signals: Iterable[RawSignal], *, output_dir: str) -> str:
    output_path = project_path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    date_label = datetime.now().astimezone().strftime("%Y%m%d")
    path = output_path / "{0}.eastmoney.jsonl".format(date_label)
    lines = [json.dumps(_to_filing_jsonl_record(signal), ensure_ascii=False, default=str) for signal in signals]
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
    content = "\n".join(lines)
    if existing:
        content = existing + "\n" + content
    path.write_text(content + "\n", encoding="utf-8")
    return str(path)


def _source_error(symbol: str, summary: str) -> RawSignal:
    return RawSignal(
        source="eastmoney_filing",
        source_type="source_error",
        title="东方财富公告抓取失败: {0}".format(symbol),
        summary=summary,
        published_at=now_iso(),
        payload={
            "filing_evidence": {
                "provider": "eastmoney_filing",
                "status": "error",
                "symbol": symbol,
                "error": summary,
                "extracted_at": now_iso(),
            }
        },
    )


def _classify_announcement(title: str, columns: List[str]) -> str:
    text = "{0} {1}".format(title, " ".join(columns))
    rules = [
        ("annual_report", ["年度报告", "年报"]),
        ("semiannual_report", ["半年度报告", "中报"]),
        ("quarterly_report", ["第一季度报告", "一季度报告", "第三季度报告", "三季度报告", "季报"]),
        ("performance_forecast", ["业绩预告", "业绩快报"]),
        ("dividend", ["权益分派", "利润分配", "分红", "派息"]),
        ("buyback", ["回购"]),
        ("shareholder_reduction", ["减持"]),
        ("financing", ["定增", "可转债", "募集资金", "发行股票"]),
        ("regulatory_inquiry", ["问询函", "监管函", "处罚", "立案"]),
        ("major_contract", ["重大合同", "中标", "订单"]),
        ("governance", ["董事", "监事", "高管", "股东大会"]),
        ("legal_opinion", ["法律意见书"]),
    ]
    for label, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return label
    return columns[0] if columns else "announcement"


def _infer_report_period(title: str) -> str:
    year_match = re.search(r"(20\d{2})\s*年?", title)
    if not year_match:
        return ""
    year = year_match.group(1)
    if any(word in title for word in ["年度报告", "年报"]):
        return "{0}A".format(year)
    if any(word in title for word in ["半年度报告", "中报"]):
        return "{0}H1".format(year)
    if any(word in title for word in ["第一季度报告", "一季度报告"]):
        return "{0}Q1".format(year)
    if any(word in title for word in ["第三季度报告", "三季度报告"]):
        return "{0}Q3".format(year)
    if any(word in title for word in ["业绩预告", "业绩快报"]):
        return "{0}E".format(year)
    return ""


def _requires_report_period(announcement_type: str) -> bool:
    return announcement_type in {"annual_report", "semiannual_report", "quarterly_report", "performance_forecast"}


def _build_key_points(title: str, columns: List[str]) -> List[str]:
    points = []
    if title:
        points.append(title)
    for column in columns:
        if column and column not in points:
            points.append(column)
    return points[:5]


def _first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    if isinstance(value, dict):
        return value
    return {}


def _clean_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return now_iso()
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", text):
        return text.replace(" ", "T")
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3}$", text):
        base, millis = text.rsplit(":", 1)
        return base.replace(" ", "T") + "." + millis
    return text
