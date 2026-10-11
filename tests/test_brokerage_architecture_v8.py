import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def _latest_report_payload():
    return {
        "generated_at": "2026-06-21T09:00:00+08:00",
        "signals": [
            {
                "signal_id": "sig-a",
                "title": "AI server contract",
                "priority": "P1",
                "source": "eastmoney_filing",
                "source_type": "filing",
                "llm_analysis": {"human_decision_needed": True, "confidence": 0.82},
                "role_synthesis": {"conflict_level": "high", "decision_gate": "human_review"},
            },
            {
                "signal_id": "sig-b",
                "title": "price snapshot",
                "priority": "P3",
                "source": "market_cache",
                "source_type": "price",
            },
        ],
        "stats": {
            "inserted": 2,
            "duplicates": 1,
            "source_health": {
                "overall_status": "WARN",
                "source_degraded_event_count": 1,
                "source_degraded_events": [
                    {"source_id": "eastmoney", "severity": "FAIL", "detail": "timeout"},
                ],
            },
            "remediation_queue": [
                {
                    "task_id": "remediation:signal:sig-a:001",
                    "task_type": "verify_official_filing",
                    "gap_type": "official_filing",
                    "source_id": "eastmoney_filing",
                    "signal_id": "sig-a",
                    "status": "open",
                    "allowed_actions": ["verify_official_filing", "attach_filing_url", "defer_signal"],
                }
            ],
            "remediation_decision_records": [
                {
                    "task_id": "remediation:signal:sig-old:001",
                    "signal_id": "sig-old",
                    "action": "defer_remediation",
                    "status": "deferred",
                    "actor": "analyst",
                }
            ],
        },
    }


def _write_latest_report(root: Path):
    out = root / "reports" / "json"
    out.mkdir(parents=True, exist_ok=True)
    (out / "20260621T090000_0800.json").write_text(
        json.dumps(_latest_report_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "watchlist.local.json").write_text("{}", encoding="utf-8")


def _patch_project_paths(root: Path):
    stack = ExitStack()
    for name in [
        "stock_agent.brokerage_architecture.project_path",
        "stock_agent.dashboard.project_path",
        "stock_agent.cli.project_path",
        "stock_agent.config.PROJECT_ROOT",
    ]:
        if name.endswith("PROJECT_ROOT"):
            stack.enter_context(patch(name, root))
        else:
            stack.enter_context(patch(name, side_effect=lambda p, r=root: r / p))
    return stack


class BrokerageArchitectureV8Tests(unittest.TestCase):
    def test_append_events_and_project_brokerage_state(self):
        from stock_agent.brokerage_architecture import append_event, project_brokerage_state

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_paths(root):
                append_event("signal_analyzed", "signal", "sig-a", {"priority": "P1"})
                append_event("remediation_created", "remediation_task", "task-a", {"status": "open", "gap_type": "missing_url"})
                append_event("source_degraded", "source", "eastmoney", {"severity": "FAIL"})
                projection = project_brokerage_state()

        self.assertEqual(projection["domains"]["research_front_office"]["signal_count"], 1)
        self.assertEqual(projection["domains"]["operations_middle_office"]["remediation_open_count"], 1)
        self.assertEqual(projection["domains"]["data_platform"]["source_degraded_count"], 1)
        self.assertEqual(projection["objects"]["signals"]["sig-a"]["status"], "analyzed")
        self.assertEqual(projection["objects"]["remediation_tasks"]["task-a"]["status"], "open")

    def test_sync_from_latest_report_writes_events_projection_and_report(self):
        from stock_agent.brokerage_architecture import sync_brokerage_architecture_from_latest_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_report(root)
            with _patch_project_paths(root):
                result = sync_brokerage_architecture_from_latest_report()
                architecture_json_exists = Path(result["architecture_json_path"]).exists()
                latest = json.loads((root / "reports" / "architecture" / "latest.json").read_text(encoding="utf-8"))
                md = (root / "reports" / "architecture" / "latest.md").read_text(encoding="utf-8-sig")
                events = list((root / "data" / "events").glob("events_*.jsonl"))

        self.assertTrue(events)
        self.assertTrue(architecture_json_exists)
        self.assertEqual(latest["domains"]["research_front_office"]["signal_count"], 2)
        self.assertEqual(latest["domains"]["operations_middle_office"]["remediation_open_count"], 1)
        self.assertIn("Brokerage Architecture State", md)
        self.assertIn("research_front_office", md)

    def test_dashboard_displays_architecture_state(self):
        from stock_agent.brokerage_architecture import sync_brokerage_architecture_from_latest_report
        from stock_agent.dashboard import _render_dashboard_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_report(root)
            with _patch_project_paths(root):
                architecture = sync_brokerage_architecture_from_latest_report()["architecture_state"]
                html = _render_dashboard_html({
                    "generated_at": "2026-06-21T09:00:00+08:00",
                    "signals": _latest_report_payload()["signals"],
                    "stats": {
                        **_latest_report_payload()["stats"],
                        "brokerage_architecture": architecture,
                    },
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

        self.assertIn("Brokerage Architecture", html)
        self.assertIn("research_front_office", html)
        self.assertIn("operations_middle_office", html)
        self.assertIn("risk_compliance", html)

    def test_cli_architecture_sync_generates_latest_projection(self):
        from stock_agent.cli import _architecture_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_report(root)
            with _patch_project_paths(root):
                _architecture_sync("config/watchlist.local.json")
                latest_exists = (root / "reports" / "architecture" / "latest.json").exists()

        self.assertTrue(latest_exists)

    def test_architecture_outputs_do_not_contain_automatic_trading_actions(self):
        from stock_agent.brokerage_architecture import sync_brokerage_architecture_from_latest_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_report(root)
            with _patch_project_paths(root):
                result = sync_brokerage_architecture_from_latest_report()

        serialized = json.dumps(result, ensure_ascii=False).lower()
        for forbidden in ["buy", "sell", "order", "rebalance", "trade"]:
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
