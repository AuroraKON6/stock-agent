import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _patch_paths(root: Path):
    stack = ExitStack()
    path_targets = [
        "stock_agent.brokerage_architecture.project_path",
        "stock_agent.source_remediation.project_path",
        "stock_agent.decisions.project_path",
        "stock_agent.research_cases.project_path",
        "stock_agent.review.project_path",
        "stock_agent.dashboard.project_path",
        "stock_agent.cli.project_path",
    ]
    for target in path_targets:
        stack.enter_context(patch(target, side_effect=lambda p, r=root: r / p))
    stack.enter_context(patch("stock_agent.config.PROJECT_ROOT", root))
    return stack


def _latest_report_payload():
    return {
        "generated_at": "2026-06-21T10:00:00+08:00",
        "signals": [
            {
                "signal_id": "sig-v9-a",
                "title": "AI server contract",
                "priority": "P1",
                "source": "eastmoney_filing",
                "source_type": "filing",
                "llm_analysis": {
                    "impact_direction": "positive",
                    "confidence": 0.8,
                    "human_decision_needed": True,
                },
                "llm_error": "",
            },
            {
                "signal_id": "sig-v9-b",
                "title": "market price snapshot",
                "priority": "P3",
                "source": "market_cache",
                "source_type": "price",
            },
        ],
        "research_cases": [
            {
                "case_id": "case-v9-a",
                "title": "AI server contract / computing power",
                "status": "active",
                "top_priority": "P1",
                "signal_count": 1,
            }
        ],
        "stats": {
            "inserted": 2,
            "duplicates": 0,
            "source_health": {
                "source_degraded_events": [
                    {"source_id": "eastmoney", "severity": "FAIL", "detail": "timeout"}
                ]
            },
            "remediation_queue": [
                {
                    "task_id": "remediation:signal:sig-v9-a:001",
                    "task_type": "verify_official_filing",
                    "gap_type": "official_filing",
                    "source_id": "eastmoney_filing",
                    "signal_id": "sig-v9-a",
                    "status": "open",
                },
                {
                    "task_id": "remediation:signal:sig-v9-a:002",
                    "task_type": "fill_missing_url",
                    "gap_type": "missing_url",
                    "source_id": "eastmoney_filing",
                    "signal_id": "sig-v9-a",
                    "status": "resolved",
                },
                {
                    "task_id": "remediation:source:eastmoney:001",
                    "task_type": "repair_source",
                    "gap_type": "source_degraded",
                    "source_id": "eastmoney",
                    "signal_id": "",
                    "status": "deferred",
                },
            ],
            "remediation_decision_records": [
                {
                    "record_id": "decision-v9-a",
                    "task_id": "remediation:signal:sig-v9-a:002",
                    "signal_id": "sig-v9-a",
                    "action": "attach_evidence_url",
                    "status": "resolved",
                    "actor": "analyst",
                }
            ],
        },
    }


def _write_latest_report(root: Path):
    out = root / "reports" / "json"
    out.mkdir(parents=True, exist_ok=True)
    (out / "20260621T100000_0800.json").write_text(
        json.dumps(_latest_report_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")


def _write_completed_review(root: Path):
    review_dir = root / "data" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "signal_id": "sig-v9-a",
        "title": "AI server contract",
        "priority": "P1",
        "was_effective": "true",
        "reviewed_at": "2026-06-21T12:00:00+08:00",
        "notes": "validated by official filing",
    }
    (review_dir / "completed_review_20260621.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_remediation_latest(root: Path):
    out = root / "reports" / "remediation"
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": "2026-06-21T10:30:00+08:00",
        "remediation_queue": [
            {
                "task_id": "remediation:signal:sig-v9-a:001",
                "task_type": "verify_official_filing",
                "gap_type": "official_filing",
                "source_id": "eastmoney_filing",
                "signal_id": "sig-v9-a",
                "status": "open",
            }
        ],
        "remediation_decision_records": [],
        "remediation_status_counts": {"open": 1},
        "open_count": 1,
    }
    (out / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class EventDrivenCoreV9Tests(unittest.TestCase):
    def test_projection_restores_signal_remediation_case_source_decision_and_review_states(self):
        from stock_agent.brokerage_architecture import project_brokerage_state

        events = [
            {
                "event_type": "signal_collected",
                "entity_type": "signal",
                "entity_id": "sig-v9-a",
                "occurred_at": "2026-06-21T09:00:00+08:00",
                "payload": {"title": "AI server contract", "priority": "P1", "source": "rss"},
            },
            {
                "event_type": "signal_analyzed",
                "entity_type": "signal",
                "entity_id": "sig-v9-a",
                "occurred_at": "2026-06-21T09:01:00+08:00",
                "payload": {"priority": "P1"},
            },
            {
                "event_type": "llm_reviewed",
                "entity_type": "signal",
                "entity_id": "sig-v9-a",
                "occurred_at": "2026-06-21T09:02:00+08:00",
                "payload": {"llm_human_decision_needed": True, "llm_confidence": 0.8},
            },
            {
                "event_type": "remediation_created",
                "entity_type": "remediation_task",
                "entity_id": "task-open",
                "occurred_at": "2026-06-21T09:03:00+08:00",
                "payload": {"status": "open", "gap_type": "missing_url"},
            },
            {
                "event_type": "remediation_resolved",
                "entity_type": "remediation_task",
                "entity_id": "task-open",
                "occurred_at": "2026-06-21T09:04:00+08:00",
                "payload": {"status": "resolved", "gap_type": "missing_url"},
            },
            {
                "event_type": "remediation_deferred",
                "entity_type": "remediation_task",
                "entity_id": "task-deferred",
                "occurred_at": "2026-06-21T09:05:00+08:00",
                "payload": {"status": "deferred"},
            },
            {
                "event_type": "research_case_updated",
                "entity_type": "research_case",
                "entity_id": "case-v9-a",
                "occurred_at": "2026-06-21T09:06:00+08:00",
                "payload": {"status": "active", "top_priority": "P1", "signal_count": 1},
            },
            {
                "event_type": "review_completed",
                "entity_type": "review",
                "entity_id": "review-v9-a",
                "occurred_at": "2026-06-21T09:07:00+08:00",
                "payload": {"signal_id": "sig-v9-a", "was_effective": "true"},
            },
            {
                "event_type": "source_degraded",
                "entity_type": "source",
                "entity_id": "eastmoney",
                "occurred_at": "2026-06-21T09:08:00+08:00",
                "payload": {"severity": "FAIL"},
            },
            {
                "event_type": "decision_recorded",
                "entity_type": "decision",
                "entity_id": "decision-v9-a",
                "occurred_at": "2026-06-21T09:09:00+08:00",
                "payload": {"signal_id": "sig-v9-a", "decision": "confirm_signal", "actor": "analyst"},
            },
        ]

        state = project_brokerage_state(events)

        self.assertEqual(state["architecture_version"], "brokerage_event_core_v2")
        self.assertEqual(state["objects"]["signals"]["sig-v9-a"]["status"], "llm_reviewed")
        self.assertTrue(state["objects"]["signals"]["sig-v9-a"]["llm_human_decision_needed"])
        self.assertEqual(state["objects"]["remediation_tasks"]["task-open"]["status"], "resolved")
        self.assertEqual(state["objects"]["remediation_tasks"]["task-deferred"]["status"], "deferred")
        self.assertEqual(state["objects"]["research_cases"]["case-v9-a"]["status"], "active")
        self.assertEqual(state["objects"]["reviews"]["review-v9-a"]["was_effective"], "true")
        self.assertEqual(state["objects"]["decisions"]["decision-v9-a"]["decision"], "confirm_signal")
        self.assertEqual(state["domains"]["research_front_office"]["llm_reviewed_signal_count"], 1)
        self.assertEqual(state["domains"]["research_front_office"]["research_case_count"], 1)
        self.assertEqual(state["domains"]["operations_middle_office"]["review_completed_count"], 1)
        self.assertEqual(state["domains"]["data_platform"]["source_degraded_count"], 1)

    def test_sync_from_latest_report_writes_complete_lifecycle_events(self):
        from stock_agent.brokerage_architecture import load_events, sync_brokerage_architecture_from_latest_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_report(root)
            _write_completed_review(root)
            with _patch_paths(root):
                result = sync_brokerage_architecture_from_latest_report()
                event_types = {event["event_type"] for event in load_events()}

        self.assertIn("signal_collected", event_types)
        self.assertIn("signal_analyzed", event_types)
        self.assertIn("llm_reviewed", event_types)
        self.assertIn("remediation_created", event_types)
        self.assertIn("remediation_resolved", event_types)
        self.assertIn("remediation_deferred", event_types)
        self.assertIn("research_case_updated", event_types)
        self.assertIn("review_completed", event_types)
        self.assertIn("decision_recorded", event_types)
        self.assertGreaterEqual(result["events_written"], 10)
        self.assertEqual(result["architecture_state"]["objects"]["signals"]["sig-v9-a"]["status"], "llm_reviewed")

    def test_manual_remediation_action_writes_event_ledger(self):
        from stock_agent.brokerage_architecture import load_events, project_brokerage_state
        from stock_agent.source_remediation import defer_remediation_task

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_remediation_latest(root)
            with _patch_paths(root):
                defer_remediation_task("remediation:signal:sig-v9-a:001", reason="wait", actor="analyst")
                events = load_events()
                state = project_brokerage_state(events)

        self.assertIn("remediation_deferred", {event["event_type"] for event in events})
        self.assertEqual(
            state["objects"]["remediation_tasks"]["remediation:signal:sig-v9-a:001"]["status"],
            "deferred",
        )

    def test_manual_signal_and_case_decisions_write_event_ledger(self):
        from stock_agent.brokerage_architecture import load_events
        from stock_agent.decisions import record_decision
        from stock_agent.research_cases import record_case_decision

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                record_decision("sig-v9-a", "confirm_signal", actor="analyst", notes="confirmed")
                record_case_decision("case-v9-a", "needs_evidence", actor="analyst", notes="more filing proof")
                events = load_events()

        event_types = {event["event_type"] for event in events}
        self.assertIn("decision_recorded", event_types)
        self.assertIn("research_case_updated", event_types)

    def test_event_cli_entrypoints_sync_state_and_log(self):
        from stock_agent.cli import _event_log, _event_state, _event_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_report(root)
            with _patch_paths(root):
                _event_sync("config/watchlist.local.json")
                _event_state("config/watchlist.local.json")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _event_log(limit=20)
                output = buffer.getvalue()
                latest_exists = (root / "reports" / "architecture" / "latest.json").exists()

        self.assertTrue(latest_exists)
        self.assertIn("signal_analyzed", output)
        self.assertIn("events shown", output)

    def test_dashboard_shows_event_driven_state_summary(self):
        from stock_agent.brokerage_architecture import project_brokerage_state
        from stock_agent.dashboard import _render_dashboard_html

        state = project_brokerage_state([
            {
                "event_type": "signal_collected",
                "entity_type": "signal",
                "entity_id": "sig-v9-a",
                "occurred_at": "2026-06-21T09:00:00+08:00",
                "payload": {"priority": "P1"},
            },
            {
                "event_type": "remediation_created",
                "entity_type": "remediation_task",
                "entity_id": "task-v9-a",
                "occurred_at": "2026-06-21T09:01:00+08:00",
                "payload": {"status": "open"},
            },
        ])
        html = _render_dashboard_html({
            "generated_at": "2026-06-21T10:00:00+08:00",
            "signals": [],
            "stats": {"brokerage_architecture": state},
            "team": {},
            "team_ops": {},
            "research_cases": [],
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
            "decision_summary": None,
            "notification_path": "",
            "last_scheduled_run": None,
        })

        self.assertIn("Event-Driven State", html)
        self.assertIn("Signal Objects", html)
        self.assertIn("Remediation Objects", html)


if __name__ == "__main__":
    unittest.main()
