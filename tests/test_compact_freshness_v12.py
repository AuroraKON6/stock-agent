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


def _write_events(root: Path, events):
    path = root / "data" / "events" / "events_20260621.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def _base_events():
    return [
        _event("evt-a-collected", "signal_collected", "signal", "sig-a", "2026-06-21T09:00:00+08:00", {"priority": "P1"}),
        _event("evt-a-analyzed", "signal_analyzed", "signal", "sig-a", "2026-06-21T09:01:00+08:00", {"priority": "P1"}),
    ]


def _new_event():
    return _event("evt-b-collected", "signal_collected", "signal", "sig-b", "2026-06-21T09:02:00+08:00", {"priority": "P2"})


def _write_report(root: Path, generated_at: str):
    out = root / "reports" / "json"
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at,
        "signals": [
            {
                "signal_id": "sig-sync-new",
                "title": "new signal after compact",
                "priority": "P2",
                "source": "manual",
                "source_type": "manual",
            }
        ],
        "stats": {"inserted": 1, "duplicates": 0},
    }
    (out / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")


class CompactFreshnessV12Tests(unittest.TestCase):
    def test_event_health_marks_compact_stale_after_new_raw_event(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, compute_event_health

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_path = _write_events(root, _base_events())
            with _patch_paths(root):
                compact_event_ledger()
                with raw_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(_new_event(), ensure_ascii=False) + "\n")
                health = compute_event_health()

        self.assertTrue(health["compact_stale"])
        self.assertTrue(health["needs_compaction"])
        self.assertEqual(health["raw_total_events"], 3)
        self.assertEqual(health["compact_source_raw_total_events"], 2)
        self.assertEqual(health["projection_source"], "raw_stale_compact")

    def test_project_state_falls_back_to_raw_when_compact_is_stale(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, project_brokerage_state

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_path = _write_events(root, _base_events())
            with _patch_paths(root):
                compact_event_ledger()
                with raw_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(_new_event(), ensure_ascii=False) + "\n")
                state = project_brokerage_state()

        self.assertEqual(state["event_health"]["projection_source"], "raw_stale_compact")
        self.assertEqual(state["domains"]["audit_back_office"]["event_count"], 3)
        self.assertIn("sig-b", state["objects"]["signals"])

    def test_sync_refreshes_existing_compact_after_writing_new_events(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, load_compact_events, sync_brokerage_architecture_from_latest_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_events(root, _base_events())
            _write_report(root, "2026-06-21T10:00:00+08:00")
            with _patch_paths(root):
                compact_event_ledger()
                before = len(load_compact_events())
                result = sync_brokerage_architecture_from_latest_report("reports/json/latest.json")
                after = len(load_compact_events())

        self.assertGreater(result["events_written"], 0)
        self.assertGreater(after, before)
        self.assertFalse(result["event_health"]["compact_stale"])

    def test_cli_health_and_dashboard_show_freshness(self):
        from stock_agent.brokerage_architecture import compact_event_ledger, project_brokerage_state
        from stock_agent.cli import _event_health
        from stock_agent.dashboard import _render_dashboard_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_path = _write_events(root, _base_events())
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")
            with _patch_paths(root):
                compact_event_ledger()
                with raw_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(_new_event(), ensure_ascii=False) + "\n")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _event_health("config/watchlist.local.json")
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

        self.assertIn("compact_stale: True", buffer.getvalue())
        self.assertIn("needs_compaction: True", buffer.getvalue())
        self.assertIn("freshness=stale", html)


if __name__ == "__main__":
    unittest.main()
