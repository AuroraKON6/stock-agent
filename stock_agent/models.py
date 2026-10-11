from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class RawSignal:
    source: str
    source_type: str
    title: str
    summary: str = ""
    url: str = ""
    published_at: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalyzedSignal:
    signal_id: str
    source: str
    source_type: str
    title: str
    summary: str
    url: str
    published_at: str
    collected_at: str
    topics: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    sentiment_score: float = 0.0
    priority_score: float = 0.0
    priority: str = "P3"
    needs_review: bool = False
    reasons: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    research_tracks: List[Dict[str, Any]] = field(default_factory=list)
    research_questions: List[str] = field(default_factory=list)
    research_context: Dict[str, Any] = field(default_factory=dict)
    team_assignments: List[Dict[str, Any]] = field(default_factory=list)
    human_decision: Dict[str, Any] = field(default_factory=dict)
    role_analyses: List[Dict[str, Any]] = field(default_factory=list)
    role_synthesis: Dict[str, Any] = field(default_factory=dict)
    role_analysis_error: str = ""
    evidence_chain: Dict[str, Any] = field(default_factory=dict)
    evidence_gap_tasks: List[Dict[str, Any]] = field(default_factory=list)
    llm_governance: Dict[str, Any] = field(default_factory=dict)
    llm_analysis: Dict[str, Any] = field(default_factory=dict)
    llm_actions: List[str] = field(default_factory=list)
    llm_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineResult:
    generated_at: str
    signals: List[AnalyzedSignal]
    alerts: List[AnalyzedSignal]
    report_path: str
    json_path: str
    alert_path: str
    stats: Dict[str, Any]
    review_path: str = ""
    actions_path: str = ""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
