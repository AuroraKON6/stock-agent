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
        "stock_agent.decisions.project_path",
        "stock_agent.cli.project_path",
    ]:
        stack.enter_context(patch(target, side_effect=lambda p, r=root: r / p))
    stack.enter_context(patch("stock_agent.config.PROJECT_ROOT", root))
    return stack


def _report_payload(generated_at: str = "2026-06-21T10:00:00+08:00"):
    return {
        "generated_at": generated_at,
        "signals": [
            {
                "signal_id": "sig-v10-a",
                "title": "AI server contract",
                "priority": "P1",
                "source": "eastmoney_filing",
                "source_type": "filing",
                "llm_analysis": {
                    "impact_direction": "positive",
                    "confidence": 0.8,
                    "human_decision_needed": True,
                },
            }
        ],
        "research_cases": [
            {
                "case_id": "case-v10-a",
                "title": "AI server contract / computing power",
                "status": "active",
                "top_priority": "P1",
                "signal_count": 1,
            }
        ],
        "stats": {
            "inserted": 1,
            "duplicates": 0,
            "source_health": {
                "source_degraded_events": [
                    {"source_id": "eastmoney", "severity": "FAIL", "detail": "timeout"}
                ]
            },
            "remediation_queue": [
                {
                    "task_id": "remediation:signal:sig-v10-a:001",
                    "task_type": "verify_official_filing",
                    "gap_type": "official_filing",
                    "source_id": "eastmoney_filing",
                    "signal_id": "sig-v10-a",
                    "status": "open",
                }
            ],
            "remediation_decision_records": [
                {
                    "record_id": "remediation-decision-v10-a",
                    "task_id": "remediation:signal:sig-v10-a:001",
                    "signal_id": "sig-v10-a",
                    "action": "defer_remediation",
                    "status": "deferred",
                    "actor": "analyst",
                }
            ],
        },
    }


def _write_report(root: Path, filename: str, generated_at: str):
    out = root / "reports" / "json"
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    path.write_text(json.dumps(_report_payload(generated_at), ensure_ascii=False), encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")
    return path


class EventLedgerReliabilityV10Tests(unittest.TestCase):
    def test_append_event_with_idempotency_key_skips_duplicate_write(self):
        from stock_agent.brokerage_architecture import append_event, load_events

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                first = append_event(
                    "signal_collected",
                    "signal",
                    "sig-v10-a",
                    {"priority": "P1"},
                    idempotency_key="run-a:signal_collected:sig-v10-a",
                )
                second = append_event(
                    "signal_collected",
                    "signal",
                    "sig-v10-a",
                    {"priority": "P1"},
                    idempotency_key="run-a:signal_collected:sig-v10-a",
                )
                events = load_events()

        self.assertFalse(first.get("duplicate_skipped", False))
        self.assertTrue(second.get("duplicate_skipped", False))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], first["event_id"])

    def test_sync_same_report_is_idempotent(self):
        from stock_agent.brokerage_architecture import load_events, sync_brokerage_architecture_from_latest_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_report(root, "report_a.json", "2026-06-21T10:00:00+08:00")
            with _patch_paths(root):
                first = sync_brokerage_architecture_from_latest_report("reports/json/report_a.json")
                count_after_first = len(load_events())
                second = sync_brokerage_architecture_from_latest_report("reports/json/report_a.json")
                count_after_second = len(load_events())

        self.assertGreater(first["events_written"], 0)
        self.assertEqual(second["events_written"], 0)
        self.assertEqual(second["duplicate_skipped"], first["events_written"])
        self.assertEqual(count_after_second, count_after_first)
        self.assertEqual(second["event_health"]["total_events"], second["event_health"]["unique_events"])

    def test_sync_different_run_appends_new_semantic_events(self):
        from stock_agent.brokerage_architecture import load_events, sync_brokerage_architecture_from_latest_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_report(root, "report_a.json", "2026-06-21T10:00:00+08:00")
            _write_report(root, "report_b.json", "2026-06-21T10:05:00+08:00")
            with _patch_paths(root):
                first = sync_brokerage_architecture_from_latest_report("reports/json/report_a.json")
                second = sync_brokerage_architecture_from_latest_report("reports/json/report_b.json")
                events = load_events()

        self.assertGreater(first["events_written"], 0)
        self.assertEqual(second["events_written"], first["events_written"])
        self.assertEqual(len(events), first["events_written"] + second["events_written"])

    def test_manual_decisions_remain_distinct_events(self):
        from stock_agent.brokerage_architecture import load_events
        from stock_agent.decisions import record_decision

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                record_decision("sig-v10-a", "confirm_signal", notes="first review", actor="analyst")
                record_decision("sig-v10-a", "request_more_data", notes="second review", actor="analyst")
                events = load_events()

        decision_events = [event for event in events if event.get("event_type") == "decision_recorded"]
        self.assertEqual(len(decision_events), 2)
        self.assertEqual(len({event["event_id"] for event in decision_events}), 2)

    def test_event_health_cli_reports_total_unique_and_duplicate_skipped(self):
        from stock_agent.brokerage_architecture import sync_brokerage_architecture_from_latest_report
        from stock_agent.cli import _event_health

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_report(root, "report_a.json", "2026-06-21T10:00:00+08:00")
            with _patch_paths(root):
                sync_brokerage_architecture_from_latest_report("reports/json/report_a.json")
                sync_brokerage_architecture_from_latest_report("reports/json/report_a.json")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _event_health("config/watchlist.local.json")
                output = buffer.getvalue()

        self.assertIn("total_events", output)
        self.assertIn("unique_events", output)
        self.assertIn("duplicate_skipped", output)


if __name__ == "__main__":
    unittest.main()
