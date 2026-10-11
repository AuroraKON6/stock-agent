from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .models import AnalyzedSignal


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    url TEXT,
    published_at TEXT,
    collected_at TEXT,
    priority TEXT,
    priority_score REAL,
    sentiment_score REAL,
    needs_review INTEGER,
    topics_json TEXT,
    symbols_json TEXT,
    keywords_json TEXT,
    reasons_json TEXT,
    payload_json TEXT,
    research_tracks_json TEXT,
    research_questions_json TEXT,
    research_context_json TEXT,
    team_assignments_json TEXT,
    human_decision_json TEXT,
    role_analyses_json TEXT,
    role_synthesis_json TEXT,
    role_analysis_error TEXT,
    evidence_chain_json TEXT,
    evidence_gap_tasks_json TEXT,
    llm_governance_json TEXT,
    llm_analysis_json TEXT,
    llm_actions_json TEXT,
    llm_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_published_at ON signals(published_at);
CREATE INDEX IF NOT EXISTS idx_signals_priority ON signals(priority);
"""

_MIGRATE_COLUMNS = [
    ("llm_analysis_json", "TEXT"),
    ("llm_actions_json", "TEXT"),
    ("llm_error", "TEXT"),
    ("research_tracks_json", "TEXT"),
    ("research_questions_json", "TEXT"),
    ("research_context_json", "TEXT"),
    ("team_assignments_json", "TEXT"),
    ("human_decision_json", "TEXT"),
    ("role_analyses_json", "TEXT"),
    ("role_synthesis_json", "TEXT"),
    ("role_analysis_error", "TEXT"),
    ("evidence_chain_json", "TEXT"),
    ("evidence_gap_tasks_json", "TEXT"),
    ("llm_governance_json", "TEXT"),
]


class SignalStore:
    def __init__(self, sqlite_path: Path):
        self.sqlite_path = sqlite_path
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(sqlite_path))
        self.conn.executescript(SCHEMA)
        self._migrate_add_columns()

    def close(self) -> None:
        self.conn.close()

    def _migrate_add_columns(self) -> None:
        """Add LLM columns to existing databases that lack them."""
        cursor = self.conn.execute("PRAGMA table_info(signals)")
        existing = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in _MIGRATE_COLUMNS:
            if col_name not in existing:
                self.conn.execute("ALTER TABLE signals ADD COLUMN {} {}".format(col_name, col_type))
        self.conn.commit()

    def upsert_many(self, signals: Iterable[AnalyzedSignal]) -> Tuple[List[AnalyzedSignal], int]:
        inserted: List[AnalyzedSignal] = []
        duplicates = 0
        for signal in signals:
            if self.exists(signal.signal_id):
                duplicates += 1
                continue
            self.insert(signal)
            inserted.append(signal)
        self.conn.commit()
        return inserted, duplicates

    def exists(self, signal_id: str) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,))
        return cursor.fetchone() is not None

    def fetch_many(self, signal_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Load persisted signals as report-shaped dictionaries for case reconstruction."""
        ids = [str(item).strip() for item in signal_ids if str(item).strip()]
        if not ids:
            return []
        rows: List[Dict[str, Any]] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self.conn.execute(
                "SELECT * FROM signals WHERE signal_id IN ({0})".format(placeholders),
                tuple(chunk),
            )
            columns = [item[0] for item in cursor.description]
            for values in cursor.fetchall():
                rows.append(_stored_signal_dict(dict(zip(columns, values))))
        return rows

    def insert(self, signal: AnalyzedSignal) -> None:
        self.conn.execute(
            """
            INSERT INTO signals (
                signal_id, source, source_type, title, summary, url, published_at, collected_at,
                priority, priority_score, sentiment_score, needs_review, topics_json, symbols_json,
                keywords_json, reasons_json, payload_json, research_tracks_json, research_questions_json,
                research_context_json, team_assignments_json, human_decision_json,
                role_analyses_json, role_synthesis_json, role_analysis_error,
                evidence_chain_json, evidence_gap_tasks_json,
                llm_governance_json, llm_analysis_json, llm_actions_json, llm_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.signal_id,
                signal.source,
                signal.source_type,
                signal.title,
                signal.summary,
                signal.url,
                signal.published_at,
                signal.collected_at,
                signal.priority,
                signal.priority_score,
                signal.sentiment_score,
                1 if signal.needs_review else 0,
                json.dumps(signal.topics, ensure_ascii=False),
                json.dumps(signal.symbols, ensure_ascii=False),
                json.dumps(signal.matched_keywords, ensure_ascii=False),
                json.dumps(signal.reasons, ensure_ascii=False),
                json.dumps(signal.payload, ensure_ascii=False, default=str),
                json.dumps(signal.research_tracks, ensure_ascii=False, default=str) if signal.research_tracks else None,
                json.dumps(signal.research_questions, ensure_ascii=False, default=str) if signal.research_questions else None,
                json.dumps(signal.research_context, ensure_ascii=False, default=str) if signal.research_context else None,
                json.dumps(signal.team_assignments, ensure_ascii=False, default=str) if signal.team_assignments else None,
                json.dumps(signal.human_decision, ensure_ascii=False, default=str) if signal.human_decision else None,
                json.dumps(signal.role_analyses, ensure_ascii=False, default=str) if signal.role_analyses else None,
                json.dumps(signal.role_synthesis, ensure_ascii=False, default=str) if signal.role_synthesis else None,
                signal.role_analysis_error or None,
                json.dumps(signal.evidence_chain, ensure_ascii=False, default=str) if signal.evidence_chain else None,
                json.dumps(signal.evidence_gap_tasks, ensure_ascii=False, default=str) if signal.evidence_gap_tasks else None,
                json.dumps(signal.llm_governance, ensure_ascii=False, default=str) if signal.llm_governance else None,
                json.dumps(signal.llm_analysis, ensure_ascii=False) if signal.llm_analysis else None,
                json.dumps(signal.llm_actions, ensure_ascii=False) if signal.llm_actions else None,
                signal.llm_error or None,
            ),
        )


_JSON_FIELD_MAP = {
    "topics_json": "topics",
    "symbols_json": "symbols",
    "keywords_json": "matched_keywords",
    "reasons_json": "reasons",
    "payload_json": "payload",
    "research_tracks_json": "research_tracks",
    "research_questions_json": "research_questions",
    "research_context_json": "research_context",
    "team_assignments_json": "team_assignments",
    "human_decision_json": "human_decision",
    "role_analyses_json": "role_analyses",
    "role_synthesis_json": "role_synthesis",
    "evidence_chain_json": "evidence_chain",
    "evidence_gap_tasks_json": "evidence_gap_tasks",
    "llm_governance_json": "llm_governance",
    "llm_analysis_json": "llm_analysis",
    "llm_actions_json": "llm_actions",
}


def _stored_signal_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        key: value
        for key, value in row.items()
        if key not in _JSON_FIELD_MAP
    }
    for column, field in _JSON_FIELD_MAP.items():
        raw = row.get(column)
        if not raw:
            result[field] = {} if field in {
                "payload",
                "research_context",
                "human_decision",
                "role_synthesis",
                "evidence_chain",
                "llm_governance",
                "llm_analysis",
            } else []
            continue
        try:
            result[field] = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            result[field] = {}
    result["needs_review"] = bool(result.get("needs_review"))
    return result
