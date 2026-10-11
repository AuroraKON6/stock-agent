from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Tuple

from .models import AnalyzedSignal, RawSignal, now_iso
from .text import normalize_text, unique_keep_order


PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def analyze_signals(raw_signals: Iterable[RawSignal], config: Dict[str, Any]) -> List[AnalyzedSignal]:
    return [analyze_one(signal, config) for signal in raw_signals]


def analyze_one(signal: RawSignal, config: Dict[str, Any]) -> AnalyzedSignal:
    text = "{0} {1}".format(signal.title, signal.summary)
    normalized = normalize_text(text)
    topics, symbols, matched_keywords, risk_hits = _match_topics(normalized, config.get("topics", []))
    sentiment_score, sentiment_reasons = _score_sentiment(normalized, config.get("priority_rules", {}))
    priority_score, priority, priority_reasons = _score_priority(
        signal=signal,
        normalized_text=normalized,
        topics=topics,
        matched_keywords=matched_keywords,
        risk_hits=risk_hits,
        sentiment_score=sentiment_score,
        rules=config.get("priority_rules", {}),
    )
    needs_review, review_reasons = _needs_review(signal, normalized, priority, rules=config.get("priority_rules", {}))
    reasons = unique_keep_order(sentiment_reasons + priority_reasons + review_reasons)
    return AnalyzedSignal(
        signal_id=signal_id(signal),
        source=signal.source,
        source_type=signal.source_type,
        title=signal.title.strip(),
        summary=signal.summary.strip(),
        url=signal.url.strip(),
        published_at=signal.published_at or now_iso(),
        collected_at=now_iso(),
        topics=topics,
        symbols=symbols,
        matched_keywords=matched_keywords,
        sentiment_score=sentiment_score,
        priority_score=priority_score,
        priority=priority,
        needs_review=needs_review,
        reasons=reasons,
        payload=signal.payload,
    )


def signal_id(signal: RawSignal) -> str:
    if signal.source_type == "price":
        price_identity = _price_snapshot_identity(signal)
        if price_identity:
            return hashlib.sha1(price_identity.encode("utf-8")).hexdigest()[:16]
    identity = "|".join(
        [
            normalize_text(signal.source),
            normalize_text(signal.title),
            normalize_text(signal.url),
            normalize_text(signal.published_at or ""),
        ]
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def _price_snapshot_identity(signal: RawSignal) -> str:
    """Return a provider-independent identity for an identical quote snapshot.

    Fetch time, cache metadata, provider name, and presentation text are excluded.
    Material quote values remain in the identity, so a genuinely changed market
    snapshot is still stored as a new observation.
    """
    payload = signal.payload if isinstance(signal.payload, dict) else {}
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    symbol = normalize_text(str(payload.get("code") or payload.get("symbol") or raw.get("f12") or ""))
    if not symbol:
        return ""

    fields = {
        "latest": _identity_scalar(payload.get("latest", raw.get("f2"))),
        "pct_change": _identity_scalar(payload.get("pct_change", raw.get("f3"))),
        "price_change": _identity_scalar(payload.get("price_change", raw.get("f4"))),
        "volume": _identity_scalar(payload.get("volume", raw.get("f5"))),
        "amount": _identity_scalar(payload.get("amount", raw.get("f6"))),
        "amplitude": _identity_scalar(payload.get("amplitude", raw.get("f7"))),
        "high": _identity_scalar(payload.get("high", raw.get("f15"))),
        "low": _identity_scalar(payload.get("low", raw.get("f16"))),
        "open": _identity_scalar(payload.get("open", raw.get("f17"))),
        "previous_close": _identity_scalar(payload.get("previous_close", raw.get("f18"))),
        "main_net_inflow": _identity_scalar(payload.get("main_net_inflow", raw.get("f62"))),
    }
    if not any(value is not None for value in fields.values()):
        return ""
    return json.dumps(
        {"kind": "price_snapshot", "symbol": symbol, "quote": fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _identity_scalar(value: Any) -> Any:
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return normalize_text(str(value))
    if not math.isfinite(number):
        return normalize_text(str(value))
    if number == 0:
        number = 0.0
    return format(number, ".15g")


def min_priority_passes(priority: str, threshold: str) -> bool:
    return PRIORITY_ORDER.get(priority, 99) <= PRIORITY_ORDER.get(threshold, 99)


def _match_topics(normalized_text: str, topics_config: Iterable[Dict[str, Any]]) -> Tuple[List[str], List[str], List[str], List[str]]:
    topics: List[str] = []
    symbols: List[str] = []
    keywords: List[str] = []
    risks: List[str] = []

    for topic in topics_config:
        topic_name = str(topic.get("name", "")).strip()
        topic_symbols = [str(item) for item in topic.get("symbols", [])]
        topic_keywords = [str(item) for item in topic.get("keywords", [])]
        topic_risks = [str(item) for item in topic.get("risk_keywords", [])]
        matched = []
        for value in topic_symbols + topic_keywords + topic_risks:
            if value and normalize_text(value) in normalized_text:
                matched.append(value)
        if matched:
            if topic_name:
                topics.append(topic_name)
            symbols.extend([symbol for symbol in topic_symbols if normalize_text(symbol) in normalized_text])
            keywords.extend(matched)
            risks.extend([risk for risk in topic_risks if normalize_text(risk) in normalized_text])

    return unique_keep_order(topics), unique_keep_order(symbols), unique_keep_order(keywords), unique_keep_order(risks)


def _score_sentiment(normalized_text: str, rules: Dict[str, Any]) -> Tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []
    positive = [str(item) for item in rules.get("positive_keywords", [])]
    negative = [str(item) for item in rules.get("negative_keywords", [])]

    positive_hits = [word for word in positive if normalize_text(word) in normalized_text]
    negative_hits = [word for word in negative if normalize_text(word) in normalized_text]
    score += min(len(positive_hits) * 0.25, 1.0)
    score -= min(len(negative_hits) * 0.25, 1.0)
    if positive_hits:
        reasons.append("正面词: {0}".format("、".join(positive_hits)))
    if negative_hits:
        reasons.append("负面词: {0}".format("、".join(negative_hits)))
    return max(min(score, 1.0), -1.0), reasons


def _score_priority(
    signal: RawSignal,
    normalized_text: str,
    topics: List[str],
    matched_keywords: List[str],
    risk_hits: List[str],
    sentiment_score: float,
    rules: Dict[str, Any],
) -> Tuple[float, str, List[str]]:
    score = 0.0
    reasons: List[str] = []

    p0_hits = [word for word in rules.get("p0_keywords", []) if normalize_text(str(word)) in normalized_text]
    p1_hits = [word for word in rules.get("p1_keywords", []) if normalize_text(str(word)) in normalized_text]

    if p0_hits:
        score += 90
        reasons.append("P0关键词: {0}".format("、".join(str(item) for item in p0_hits)))
    if p1_hits:
        score += 55
        reasons.append("P1关键词: {0}".format("、".join(str(item) for item in p1_hits)))
    if topics:
        score += 12 * len(topics)
        reasons.append("命中主题: {0}".format("、".join(topics)))
    if risk_hits:
        score += min(len(risk_hits) * 24, 48)
        reasons.append("风险词: {0}".format("、".join(risk_hits)))
    if matched_keywords:
        score += min(len(matched_keywords) * 4, 20)
    if signal.source_type in ["filing", "price", "source_error"]:
        score += 8
    if abs(sentiment_score) >= 0.5:
        score += 10

    # Price anomaly detection for price signals
    price_score, price_reasons = _score_price_anomaly(signal, topics, rules)
    if price_score > 0:
        score += price_score
        reasons.extend(price_reasons)

    filing_score, filing_reasons = _score_filing_importance(signal, rules)
    if filing_score > 0:
        score += filing_score
        reasons.extend(filing_reasons)

    priority = "P3"
    if p0_hits:
        priority = "P0"
    elif score >= 55:
        priority = "P1"
    elif score >= 25:
        priority = "P2"
    return score, priority, reasons


def _score_filing_importance(signal: RawSignal, rules: Dict[str, Any]) -> Tuple[float, List[str]]:
    payload = signal.payload or {}
    filing_evidence = payload.get("filing_evidence")
    if signal.source_type != "filing" and not isinstance(filing_evidence, dict):
        return 0.0, []

    cfg = rules.get("filing_importance_rules", {})
    if cfg.get("enabled", True) is False:
        return 0.0, []

    filing = filing_evidence if isinstance(filing_evidence, dict) else {}
    announcement_type = str(payload.get("announcement_type") or filing.get("announcement_type") or "unknown")
    status = str(filing.get("status") or "unknown")
    missing_fields = [str(item) for item in filing.get("missing_fields", []) or []]
    human_review_required = bool(filing.get("human_review_required"))

    high_types = set(str(item) for item in cfg.get("high_types", [
        "performance_forecast",
        "buyback",
        "shareholder_reduction",
        "dividend",
        "regulatory_inquiry",
        "major_contract",
    ]))
    medium_types = set(str(item) for item in cfg.get("medium_types", [
        "annual_report",
        "semiannual_report",
        "quarterly_report",
        "financing",
    ]))
    low_types = set(str(item) for item in cfg.get("low_types", [
        "governance",
        "legal_opinion",
        "routine",
        "monthly_report",
    ]))

    high_score = float(cfg.get("high_score", 70))
    medium_score = float(cfg.get("medium_score", 45))
    low_score = float(cfg.get("low_score", 10))
    abnormal_score = float(cfg.get("abnormal_evidence_score", 65))

    if status not in ("ok", "") or missing_fields or human_review_required:
        level = "unknown"
        importance_score = abnormal_score
        priority_bonus = 30.0
        reasons = ["公告证据异常: {0}".format("、".join(missing_fields) if missing_fields else status)]
    elif announcement_type in high_types:
        level = "high"
        importance_score = high_score
        priority_bonus = 60.0
        reasons = ["公告重要性 high: {0}".format(announcement_type)]
    elif announcement_type in medium_types:
        level = "medium"
        importance_score = medium_score
        priority_bonus = 0.0
        reasons = ["公告重要性 medium: {0}".format(announcement_type)]
    elif announcement_type in low_types:
        level = "low"
        importance_score = low_score
        priority_bonus = 0.0
        reasons = ["公告重要性 low: {0}".format(announcement_type)]
    else:
        level = "unknown"
        importance_score = float(cfg.get("unknown_score", 25))
        priority_bonus = 0.0
        reasons = ["公告重要性 unknown: {0}".format(announcement_type)]

    payload["filing_importance_score"] = importance_score
    payload["filing_importance_level"] = level
    payload["filing_importance_reasons"] = reasons[:]
    if isinstance(filing_evidence, dict):
        filing_evidence["importance_score"] = importance_score
        filing_evidence["importance_level"] = level
        filing_evidence["importance_reasons"] = reasons[:]
    return priority_bonus, reasons


def _score_price_anomaly(signal: RawSignal, topics: List[str], rules: Dict[str, Any]) -> Tuple[float, List[str]]:
    if signal.source_type != "price":
        return 0.0, []
    payload = signal.payload or {}
    pct_change = payload.get("pct_change")
    main_net_inflow = payload.get("main_net_inflow")
    if pct_change is None and main_net_inflow is None:
        return 0.0, []

    price_rules = rules.get("price_rules", {})
    pct_threshold = float(price_rules.get("pct_change_abs_threshold", 3.0))
    inflow_threshold = float(price_rules.get("main_net_inflow_threshold", 100000000))
    outflow_threshold = float(price_rules.get("main_net_outflow_threshold", -100000000))

    anomalies = 0
    reasons: List[str] = []

    if pct_change is not None:
        pct_val = float(pct_change)
        if pct_val >= pct_threshold:
            anomalies += 1
            reasons.append("价格显著上涨({0:.2f}%)".format(pct_val))
        elif pct_val <= -pct_threshold:
            anomalies += 1
            reasons.append("价格显著下跌({0:.2f}%)".format(pct_val))

    if main_net_inflow is not None:
        inflow_val = float(main_net_inflow)
        if inflow_val >= inflow_threshold:
            anomalies += 1
            reasons.append("主力资金显著流入({0:.0f})".format(inflow_val))
        elif inflow_val <= outflow_threshold:
            anomalies += 1
            reasons.append("主力资金显著流出({0:.0f})".format(inflow_val))

    if anomalies == 0:
        return 0.0, []

    topic_match = bool(topics)
    if anomalies >= 2 and topic_match:
        priority_label = price_rules.get("priority_for_multi_price_anomaly", "P1")
        score = 60
        reasons.append("多指标异常+主题命中→{0}".format(priority_label))
    else:
        priority_label = price_rules.get("priority_for_single_price_anomaly", "P2")
        score = 30
        reasons.append("价格异常→{0}".format(priority_label))

    return score, reasons


def _needs_review(signal: RawSignal, normalized_text: str, priority: str, rules: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    review_hits = [word for word in rules.get("review_keywords", []) if normalize_text(str(word)) in normalized_text]
    if review_hits:
        reasons.append("需核验词: {0}".format("、".join(str(item) for item in review_hits)))
    if priority in ["P0", "P1"]:
        reasons.append("高优先级信号需人工确认")
    if not signal.url:
        reasons.append("缺少URL或原始来源")
    if signal.source_type == "source_error":
        reasons.append("数据源异常")
    filing = (signal.payload or {}).get("filing_evidence", {})
    if isinstance(filing, dict):
        status = str(filing.get("status") or "")
        missing = filing.get("missing_fields", []) or []
        if status not in ("", "ok") or missing or filing.get("human_review_required"):
            reasons.append("公告证据异常需人工复核")
    return bool(reasons), reasons
