from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from .models import AnalyzedSignal, now_iso
from .text import unique_keep_order


RAW_SIGNAL_FIELDS = {"signal_id", "source", "source_type", "title", "summary", "url", "published_at", "collected_at"}
MARKET_FIELDS = {"latest", "pct_change", "price_change", "main_net_inflow", "amount", "volume", "turnover"}
FINANCIAL_PREFIXES = ("financial_evidence", "metrics", "filing_evidence.metrics")
FILING_PREFIXES = ("filing_evidence", "announcement", "official_filing")
NEWS_PREFIXES = ("news_evidence", "source_evidence")


def attach_evidence_chains(signals: List[AnalyzedSignal]) -> Tuple[List[AnalyzedSignal], Dict[str, Any]]:
    for signal in signals:
        chain = build_evidence_chain(signal)
        signal.evidence_chain = chain
        signal.evidence_gap_tasks = chain.get("evidence_gap_tasks", [])
    summary = summarize_evidence_chains(signals)
    return signals, {
        "evidence_chain_signal_count": summary["signal_count"],
        "evidence_chain_avg_score": summary["avg_score"],
        "evidence_gap_task_count": summary["gap_task_count"],
        "evidence_chain_status_counts": summary["status_counts"],
        "evidence_chain_summary": summary,
    }


def build_evidence_chain(signal: AnalyzedSignal) -> Dict[str, Any]:
    refs = _collect_refs(signal)
    resolved_refs: List[Dict[str, Any]] = []
    unresolved_refs: List[str] = []
    for ref in refs:
        resolved = _resolve_ref(signal, ref)
        if resolved:
            resolved_refs.append(resolved)
        else:
            unresolved_refs.append(ref)

    gaps = unique_keep_order(list(unresolved_refs) + _collect_missing_info(signal))
    tasks = [_gap_task(signal, gap, idx) for idx, gap in enumerate(gaps, 1)]
    quality = _quality(len(refs), len(resolved_refs), len(tasks))
    return {
        "signal_id": signal.signal_id,
        "generated_at": now_iso(),
        "resolved_refs": resolved_refs,
        "unresolved_refs": unresolved_refs,
        "evidence_gap_tasks": tasks,
        "quality": quality,
        "source_url": signal.url,
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }


def summarize_evidence_chains(signals: Iterable[Any]) -> Dict[str, Any]:
    chains = [_chain_from_signal(signal) for signal in signals]
    chains = [chain for chain in chains if chain]
    status_counts: Dict[str, int] = {}
    total_score = 0.0
    gap_count = 0
    unresolved_count = 0
    for chain in chains:
        quality = chain.get("quality", {}) or {}
        status = str(quality.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
        try:
            total_score += float(quality.get("score", 0.0))
        except (TypeError, ValueError):
            pass
        gap_count += len(chain.get("evidence_gap_tasks", []) or [])
        unresolved_count += len(chain.get("unresolved_refs", []) or [])
    count = len(chains)
    return {
        "signal_count": count,
        "avg_score": total_score / count if count else 0.0,
        "gap_task_count": gap_count,
        "unresolved_ref_count": unresolved_count,
        "status_counts": status_counts,
    }


def _collect_refs(signal: AnalyzedSignal) -> List[str]:
    refs: List[str] = []
    if signal.url:
        refs.append("url")
    if signal.summary:
        refs.append("summary")
    for role in signal.role_analyses:
        refs.extend(str(item) for item in role.get("evidence_refs", []) or [])
    llm_refs = signal.llm_analysis.get("evidence_refs") if isinstance(signal.llm_analysis, dict) else []
    if isinstance(llm_refs, list):
        refs.extend(str(item) for item in llm_refs)
    return unique_keep_order(ref.strip() for ref in refs if str(ref).strip())


def _collect_missing_info(signal: AnalyzedSignal) -> List[str]:
    gaps: List[str] = []
    for role in signal.role_analyses:
        gaps.extend(str(item) for item in role.get("missing_info", []) or [])
    if signal.research_context:
        gaps.extend(str(item) for item in signal.research_context.get("evidence_gaps", []) or [])
    return unique_keep_order(gap.strip() for gap in gaps if str(gap).strip())


def _resolve_ref(signal: AnalyzedSignal, ref: str) -> Dict[str, Any] | None:
    value = None
    category = _category_for_ref(ref)
    if ref in RAW_SIGNAL_FIELDS:
        value = getattr(signal, ref, None)
    if value is None:
        value = _nested_get(signal.payload or {}, ref)
    if value is None:
        value = _nested_get(signal.to_dict(), ref)
    if value in (None, "", [], {}):
        return None
    return {
        "ref": ref,
        "category": category,
        "value": value,
        "source": signal.source,
        "url": signal.url if ref == "url" or category in {"filing", "news"} else "",
    }


def _nested_get(data: Dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _category_for_ref(ref: str) -> str:
    if ref in RAW_SIGNAL_FIELDS:
        return "raw_signal"
    if ref in MARKET_FIELDS:
        return "market"
    if ref.startswith(FINANCIAL_PREFIXES):
        return "financial"
    if ref.startswith(FILING_PREFIXES):
        return "filing"
    if ref.startswith(NEWS_PREFIXES):
        return "news"
    return "unknown"


def _gap_task(signal: AnalyzedSignal, gap: str, idx: int) -> Dict[str, Any]:
    return {
        "task_id": "{0}:gap:{1:03d}".format(signal.signal_id, idx),
        "signal_id": signal.signal_id,
        "gap": gap,
        "status": "open",
        "owner": "human",
        "created_at": now_iso(),
        "allowed_decisions": ["confirm_signal", "reject_signal", "defer_signal", "request_more_data"],
    }


def _quality(total_refs: int, resolved_refs: int, gap_count: int) -> Dict[str, Any]:
    if total_refs <= 0:
        score = 0.0
    else:
        score = resolved_refs / float(total_refs)
    if gap_count:
        score = max(0.0, score - min(0.4, gap_count * 0.05))
    status = "OK" if score >= 0.8 and gap_count == 0 else "WARN" if score >= 0.3 else "FAIL"
    return {
        "score": round(score, 3),
        "status": status,
        "total_refs": total_refs,
        "resolved_refs": resolved_refs,
        "gap_count": gap_count,
    }


def _chain_from_signal(signal: Any) -> Dict[str, Any]:
    if isinstance(signal, dict):
        return signal.get("evidence_chain", {}) or {}
    return getattr(signal, "evidence_chain", {}) or {}
