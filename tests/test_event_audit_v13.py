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
        _event("evt-a-collect", "signal_collected", "signal", "sig-a", "2026-06-21T09:00:00+08:00", {"priority": "P1", "title": "A"}),
        _event("evt-a-analyze", "signal_analyzed", "signal", "sig-a", "2026-06-21T09:01:00+08:00", {"priority": "P1", "title": "A"}),
        _event("evt-b-collect", "signal_collected", "signal", "sig-b", "2026-06-21T09:02:00+08:00", {"priority": "P2", "title": "B"}),
        _event("evt-task-open", "remediation_created", "remediation_task", "task-a", "2026-06-21T09:03:00+08:00", {"status": "open"}),
    ]
    path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")


class EventAuditV13Tests(unittest.TestCase):
    def test_get_event_timeline_filters_entity_and_returns_current_state(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, get_event_timeline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            with _patch_paths(root):
                compact_event_ledger()
                timeline = get_event_timeline("signal", "sig-a", limit=10)

        self.assertEqual(timeline["event_count"], 2)
        self.assertEqual([event["event_type"] for event in timeline["events"]], ["signal_collected", "signal_analyzed"])
        self.assertEqual(timeline["current_state"]["status"], "analyzed")
        self.assertEqual(timeline["projection_source"], "compact")

    def test_write_event_audit_writes_json_markdown_and_latest_paths(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, write_event_audit

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            with _patch_paths(root):
                compact_event_ledger()
                result = write_event_audit("remediation_task", "task-a", limit=5)
                audit_json_exists = Path(result["event_audit_json_path"]).exists()
                latest_json = json.loads((root / "reports" / "architecture" / "event_audit_latest.json").read_text(encoding="utf-8"))
                latest_md = (root / "reports" / "architecture" / "event_audit_latest.md").read_text(encoding="utf-8-sig")

        self.assertTrue(audit_json_exists)
        self.assertEqual(latest_json["entity_type"], "remediation_task")
        self.assertEqual(latest_json["entity_id"], "task-a")
        self.assertEqual(latest_json["current_state"]["status"], "open")
        self.assertIn("Event Audit Timeline", latest_md)
        self.assertIn("remediation_created", latest_md)

    def test_cli_event_audit_generates_report_and_prints_summary(self):
        from stock_agent.brokerage_architecture import compact_event_ledger
        from stock_agent.cli import _event_audit

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")
            with _patch_paths(root):
                compact_event_ledger()
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _event_audit("config/watchlist.local.json", "signal", "sig-a", 10)
                output = buffer.getvalue()
                latest_exists = (root / "reports" / "architecture" / "event_audit_latest.json").exists()

        self.assertTrue(latest_exists)
        self.assertIn("event audit:", output)
        self.assertIn("events: 2", output)
        self.assertIn("signal:sig-a", output)

    def test_dashboard_displays_recent_events_and_latest_audit_path(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, project_brokerage_state, write_event_audit
        from stock_agent.dashboard import _render_dashboard_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_raw_events(root)
            with _patch_paths(root):
                compact_event_ledger()
                write_event_audit("signal", "sig-a", limit=10)
                state = project_brokerage_state()
                html = _render_dashboard_html({
                    "generated_at": "2026-06-22T10:00:00+08:00",
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

        self.assertIn("Recent Events", html)
        self.assertIn("signal_analyzed", html)
        self.assertIn("event_audit_latest.json", html)


if __name__ == "__main__":
    unittest.main()
