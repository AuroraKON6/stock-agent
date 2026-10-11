import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _patch_paths(root: Path):
    stack = ExitStack()
    for target in [
        "stock_agent.brokerage_architecture.project_path",
        "stock_agent.dashboard.project_path",
        "stock_agent.cli.project_path",
    ]:
        stack.enter_context(patch(target, side_effect=lambda p, r=root: r / p))
    stack.enter_context(patch("stock_agent.config.PROJECT_ROOT", root))
    return stack


def _event(event_id, event_type, entity_type, entity_id, occurred_at, payload=None):
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source": "test",
        "actor": "system",
        "correlation_id": "run-a",
        "idempotency_key": event_id,
        "payload": payload or {},
        "safety_boundary": "research only",
    }


def _write_raw_events(root: Path):
    path = root / "data" / "events" / "events_20260621.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _event("evt-signal-collected", "signal_collected", "signal", "sig-a", "2026-06-21T09:00:00+08:00", {"priority": "P1"}),
        _event("evt-signal-collected", "signal_collected", "signal", "sig-a", "2026-06-21T09:00:01+08:00", {"priority": "P1"}),
        _event("evt-signal-analyzed", "signal_analyzed", "signal", "sig-a", "2026-06-21T09:01:00+08:00", {"priority": "P1"}),
        _event("evt-remediation-open", "remediation_created", "remediation_task", "task-a", "2026-06-21T09:02:00+08:00", {"status": "open"}),
        _event("evt-remediation-open", "remediation_created", "remediation_task", "task-a", "2026-06-21T09:02:01+08:00", {"status": "open"}),
    ]
    text = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"
    path.write_text(text, encoding="utf-8")
    return path, text


class EventCompactionV11Tests(unittest.TestCase):
    def test_compact_event_ledger_preserves_raw_and_writes_unique_latest(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, load_compact_events

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_path, raw_before = _write_raw_events(root)
            with _patch_paths(root):
                result = compact_event_ledger()
                compact_events = load_compact_events()
                raw_after = raw_path.read_text(encoding="utf-8")
                compact_latest_exists = Path(result["compact_latest_path"]).exists()

        self.assertEqual(raw_after, raw_before)
        self.assertEqual(result["raw_total_events"], 5)
        self.assertEqual(result["compact_total_events"], 3)
        self.assertEqual(result["duplicate_rows"], 2)
        self.assertTrue(compact_latest_exists)
        self.assertEqual([event["event_id"] for event in compact_events], [
            "evt-signal-collected",
            "evt-signal-analyzed",
            "evt-remediation-open",
        ])

    def test_project_state_uses_compact_ledger_when_available(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, project_brokerage_state

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            with _patch_paths(root):
                compact_event_ledger()
                state = project_brokerage_state()

        audit = state["domains"]["audit_back_office"]
        health = state["event_health"]
        self.assertEqual(audit["event_count"], 3)
        self.assertEqual(audit["raw_event_count"], 5)
        self.assertEqual(audit["compact_event_count"], 3)
        self.assertEqual(health["projection_source"], "compact")
        self.assertEqual(health["raw_duplicate_rows"], 2)

    def test_cli_event_compact_and_event_health_report_compact_status(self):
        from stock_agent.cli import _event_compact, _event_health

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")
            with _patch_paths(root):
                compact_buffer = io.StringIO()
                with redirect_stdout(compact_buffer):
                    _event_compact("config/watchlist.local.json")
                health_buffer = io.StringIO()
                with redirect_stdout(health_buffer):
                    _event_health("config/watchlist.local.json")

        self.assertIn("compact_total_events: 3", compact_buffer.getvalue())
        self.assertIn("raw_total_events: 5", health_buffer.getvalue())
        self.assertIn("compact_total_events: 3", health_buffer.getvalue())
        self.assertIn("projection_source: compact", health_buffer.getvalue())

    def test_dashboard_displays_compact_ledger_health(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, project_brokerage_state
        from stock_agent.dashboard import _render_dashboard_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            with _patch_paths(root):
                compact_event_ledger()
                state = project_brokerage_state()
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

        self.assertIn("Ledger Health", html)
        self.assertIn("raw=5", html)
        self.assertIn("compact=3", html)
        self.assertIn("duplicates=2", html)


if __name__ == "__main__":
    unittest.main()
