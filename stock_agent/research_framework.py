from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

from .analysis import min_priority_passes
from .models import AnalyzedSignal
from .text import unique_keep_order


DEFAULT_PLAYBOOKS: List[Dict[str, Any]] = [
    {
        "id": "source_health",
        "label": "Source Health",
        "source_types": ["source_error"],
        "focus": ["coverage", "data_quality", "fallback"],
        "questions": [
            "数据源异常是否影响今日覆盖面？",
            "是否需要切换备用数据源或人工补录？",
        ],
    },
    {
        "id": "market_price_action",
        "label": "Market Price Action",
        "source_types": ["price"],
        "focus": ["price", "volume", "fund_flow", "confirmation"],
        "questions": [
            "价格、成交额、资金流是否同时出现异常？",
            "这只是日内波动，还是已有事件催化？",
        ],
    },
    {
        "id": "event_impact",
        "label": "Event Impact",
        "source_types": ["news", "filing", "social", "manual"],
        "focus": ["event_type", "fundamentals", "horizon", "evidence"],
        "questions": [
            "事件对收入、利润、估值或风险偏好的影响路径是什么？",
            "需要哪些公告、财报或权威来源来验证？",
        ],
    },
    {
        "id": "risk_challenge",
        "label": "Risk Challenge",
        "min_priority": "P1",
        "requires_review": True,
        "focus": ["downside", "counter_evidence", "human_review"],
        "questions": [
            "这个信号最可能错在哪里？",
            "哪些信息缺失会导致误判或过度反应？",
        ],
    },
    {
        "id": "llm_deep_dive",
        "label": "LLM Deep Dive",
        "min_priority": "P2",
        "focus": ["thesis", "risk_points", "review_questions"],
        "questions": [
            "请形成一句话核心判断，并列出需要人工复核的问题。",
            "不要给出自动交易、下单或调仓动作。",
        ],
    },
]


def apply_research_framework(
    signals: List[AnalyzedSignal],
    config: Dict[str, Any],
) -> Tuple[List[AnalyzedSignal], Dict[str, Any]]:
    framework_cfg = config.get("research_framework", {})
    enabled = framework_cfg.get("enabled", True)
    stats: Dict[str, Any] = {
        "research_framework_enabled": bool(enabled),
        "research_playbooks": 0,
        "research_signal_count": 0,
        "research_track_counts": {},
        "research_agenda": [],
    }
    if not enabled:
        return signals, stats

    playbooks = _load_playbooks(framework_cfg)
    stats["research_playbooks"] = len(playbooks)

    track_counter: Counter = Counter()
    agenda: List[str] = []
    routed_signals = 0
    for signal in signals:
        tracks = [_track_from_playbook(playbook) for playbook in playbooks if _matches(signal, playbook)]
        signal.research_tracks = tracks
        signal.research_questions = unique_keep_order(
            question
            for track in tracks
            for question in track.get("questions", [])
            if question
        )
        signal.research_context = _build_signal_context(signal, tracks)
        if tracks:
            routed_signals += 1
            for track in tracks:
                track_counter[track["id"]] += 1
                agenda.append("{0}: {1}".format(track["label"], signal.title))

    stats["research_signal_count"] = routed_signals
    stats["research_track_counts"] = dict(track_counter)
    stats["research_agenda"] = agenda[:20]
    return signals, stats


def _load_playbooks(framework_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = str(framework_cfg.get("mode") or "default_plus_config").strip()
    custom = [item for item in framework_cfg.get("playbooks", []) if isinstance(item, dict)]
    if mode == "config_only":
        return custom
    if mode == "default_only":
        return list(DEFAULT_PLAYBOOKS)
    return list(DEFAULT_PLAYBOOKS) + custom


def _matches(signal: AnalyzedSignal, playbook: Dict[str, Any]) -> bool:
    if playbook.get("enabled", True) is False:
        return False
    if playbook.get("match_all"):
        return True

    checks: List[bool] = []
    if playbook.get("source_types"):
        checks.append(signal.source_type in set(str(item) for item in playbook.get("source_types", [])))
    if playbook.get("sources"):
        checks.append(signal.source in set(str(item) for item in playbook.get("sources", [])))
    if playbook.get("priorities"):
        checks.append(signal.priority in set(str(item) for item in playbook.get("priorities", [])))
    if playbook.get("min_priority"):
        checks.append(min_priority_passes(signal.priority, str(playbook.get("min_priority"))))
    if playbook.get("requires_review") is True:
        checks.append(bool(signal.needs_review))
    if playbook.get("topic_required") is True:
        checks.append(bool(signal.topics))
    if playbook.get("symbol_required") is True:
        checks.append(bool(signal.symbols))
    if playbook.get("reason_contains"):
        haystack = " ".join(signal.reasons).lower()
        checks.append(any(str(item).lower() in haystack for item in playbook.get("reason_contains", [])))
    if playbook.get("title_contains"):
        title = signal.title.lower()
        checks.append(any(str(item).lower() in title for item in playbook.get("title_contains", [])))
    if playbook.get("payload_fields_present"):
        checks.append(
            all(signal.payload.get(str(field)) is not None for field in playbook.get("payload_fields_present", []))
        )

    return bool(checks) and all(checks)


def _track_from_playbook(playbook: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(playbook.get("id") or "unnamed_playbook"),
        "label": str(playbook.get("label") or playbook.get("id") or "Unnamed Playbook"),
        "focus": [str(item) for item in playbook.get("focus", [])],
        "questions": [str(item) for item in playbook.get("questions", [])],
    }


def _build_signal_context(signal: AnalyzedSignal, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "track_ids": [track["id"] for track in tracks],
        "track_labels": [track["label"] for track in tracks],
        "focus": unique_keep_order(
            focus for track in tracks for focus in track.get("focus", []) if focus
        ),
        "needs_human_review": signal.needs_review,
        "priority": signal.priority,
        "source_type": signal.source_type,
        "evidence_gaps": _evidence_gaps(signal),
    }


def _evidence_gaps(signal: AnalyzedSignal) -> List[str]:
    gaps: List[str] = []
    if not signal.url:
        gaps.append("missing_url")
    if signal.needs_review:
        gaps.append("human_review_required")
    if signal.source_type == "source_error":
        gaps.append("source_error")
    if not signal.topics:
        gaps.append("topic_not_matched")
    return gaps
