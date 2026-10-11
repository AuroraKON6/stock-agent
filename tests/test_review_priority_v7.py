import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def _remediation_payload():
    return {
        "generated_at": "2026-06-20T10:00:00+08:00",
        "source_degraded_event_count": 1,
        "source_degraded_events": [
            {"source_id": "eastmoney", "severity": "FAIL", "detail": "timeout"},
        ],
        "remediation_queue": [
            {
                "task_id": "remediation:signal:s_high:001",
                "task_type": "verify_official_filing",
                "gap_type": "official_filing",
                "gap": "official_filing_url",
                "source_id": "eastmoney_filing",
                "signal_id": "s_high",
                "status": "open",
                "allowed_actions": ["verify_official_filing", "attach_filing_url", "confirm_signal", "reject_signal", "defer_signal"],
                "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
            },
            {
                "task_id": "remediation:source:eastmoney:001",
                "task_type": "repair_source",
                "gap_type": "source_degraded",
                "gap": "source_degraded",
                "source_id": "eastmoney",
                "signal_id": "",
                "status": "open",
                "allowed_actions": ["retry_source", "enable_fallback_source", "open_source_health_report", "defer_signal"],
                "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
            },
            {
                "task_id": "remediation:signal:s_mid:001",
                "task_type": "fill_missing_url",
                "gap_type": "missing_url",
                "gap": "missing_url",
                "source_id": "rss",
                "signal_id": "s_mid",
                "status": "deferred",
                "allowed_actions": ["attach_evidence_url", "verify_source_url", "defer_signal"],
                "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
            },
            {
                "task_id": "remediation:signal:s_low:001",
                "task_type": "fill_missing_fields",
                "gap_type": "missing_fields",
                "gap": "missing_fields",
                "source_id": "local_jsonl",
                "signal_id": "s_low",
                "status": "resolved",
                "allowed_actions": ["attach_structured_fields", "request_more_data", "defer_signal"],
                "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
            },
        ],
        "remediation_decision_records": [
            {
                "task_id": "remediation:signal:s_mid:001",
                "signal_id": "s_mid",
                "source_id": "rss",
                "gap_type": "missing_url",
                "task_type": "fill_missing_url",
                "action": "defer_remediation",
                "status": "deferred",
                "recorded_at": "2026-06-20T09:00:00+08:00",
            },
            {
                "task_id": "remediation:signal:s_mid:001",
                "signal_id": "s_mid",
                "source_id": "rss",
                "gap_type": "missing_url",
                "task_type": "fill_missing_url",
                "action": "defer_remediation",
                "status": "deferred",
                "recorded_at": "2026-06-20T09:10:00+08:00",
            },
        ],
        "source_failure_counts": {"eastmoney": 1},
    }


def _latest_json_report():
    return {
        "signals": [
            {
                "signal_id": "s_high",
                "priority": "P0",
                "source": "eastmoney_filing",
                "title": "high priority filing",
                "llm_analysis": {"human_decision_needed": True, "confidence": 0.83},
                "role_synthesis": {"conflict_level": "high", "key_disagreement": "valuation impact"},
            },
            {
                "signal_id": "s_mid",
                "priority": "P2",
                "source": "rss",
                "title": "medium news",
                "llm_analysis": {"human_decision_needed": False, "confidence": 0.72},
                "role_synthesis": {"conflict_level": "medium"},
            },
            {
                "signal_id": "s_low",
                "priority": "P3",
                "source": "local_jsonl",
                "title": "low resolved item",
                "llm_analysis": {},
                "role_synthesis": {},
            },
        ],
        "stats": {},
    }


def _write_fixture(root: Path):
    remediation_dir = root / "reports" / "remediation"
    remediation_dir.mkdir(parents=True, exist_ok=True)
    (remediation_dir / "latest.json").write_text(json.dumps(_remediation_payload(), ensure_ascii=False), encoding="utf-8")
    json_dir = root / "reports" / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / "20260620T100000_0800.json").write_text(json.dumps(_latest_json_report(), ensure_ascii=False), encoding="utf-8")
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "watchlist.local.json").write_text("{}", encoding="utf-8")


def _patch_project_paths(root: Path):
    stack = ExitStack()
    for name in [
        "stock_agent.review_priority.project_path",
        "stock_agent.review_workbench.project_path",
        "stock_agent.dashboard.project_path",
        "stock_agent.cli.project_path",
        "stock_agent.config.PROJECT_ROOT",
    ]:
        if name.endswith("PROJECT_ROOT"):
            stack.enter_context(patch(name, root))
        else:
            stack.enter_context(patch(name, side_effect=lambda p, r=root: r / p))
    return stack


class ReviewPriorityV7Tests(unittest.TestCase):
    def test_priority_queue_scores_risks_and_sorts_top10(self):
        from stock_agent.review_priority import build_priority_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_fixture(root)
            with _patch_project_paths(root):
                queue = build_priority_queue()

        tasks = queue["tasks"]
        self.assertEqual(tasks[0]["task_id"], "remediation:signal:s_high:001")
        self.assertGreaterEqual(tasks[0]["review_priority_score"], 70)
        self.assertEqual(tasks[0]["evidence_risk_level"], "high")
        self.assertIn("signal priority P0", tasks[0]["priority_reasons"])
        self.assertEqual(queue["top10"], tasks[:10])
        scores = [task["review_priority_score"] for task in tasks]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(queue["risk_counts"]["high"], 1)
        self.assertGreaterEqual(queue["risk_counts"]["medium"], 1)
        self.assertIn("forbidden_actions_note", queue)

    def test_write_priority_queue_outputs_latest_and_timestamped_json(self):
        from stock_agent.review_priority import build_priority_queue, write_priority_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_fixture(root)
            with _patch_project_paths(root):
                paths = write_priority_queue(build_priority_queue())
                latest = json.loads((root / "reports" / "workbench" / "priority_queue_latest.json").read_text(encoding="utf-8"))

        self.assertTrue(Path(paths["priority_queue_path"]).name.startswith("priority_queue_"))
        self.assertEqual(Path(paths["priority_queue_latest_path"]).name, "priority_queue_latest.json")
        self.assertEqual(latest["top10"][0]["evidence_risk_level"], "high")

    def test_workbench_filters_by_risk_and_displays_top10(self):
        from stock_agent.review_workbench import load_workbench_state, render_workbench_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_fixture(root)
            with _patch_project_paths(root):
                state = load_workbench_state(status="all", risk="high")
                html = render_workbench_html(status="all", risk="high")

        self.assertEqual(state["risk"], "high")
        self.assertTrue(state["tasks"])
        self.assertTrue(all(task["evidence_risk_level"] == "high" for task in state["tasks"]))
        self.assertIn("Today Priority Top 10", html)
        self.assertIn("?status=all&amp;risk=high", html)
        self.assertIn("review_priority_score", html)

    def test_dashboard_displays_priority_top10(self):
        from stock_agent.dashboard import _render_dashboard_html
        from stock_agent.review_priority import build_priority_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_fixture(root)
            with _patch_project_paths(root):
                priority_queue = build_priority_queue()
                html = _render_dashboard_html({
                    "generated_at": "2026-06-20T10:00:00+08:00",
                    "signals": _latest_json_report()["signals"],
                    "stats": {
                        **_remediation_payload(),
                        "review_priority_queue": priority_queue,
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

        self.assertIn("Today Priority Top 10", html)
        self.assertIn("remediation:signal:s_high:001", html)
        self.assertIn("high", html)

    def test_cli_review_priority_generates_latest_json(self):
        from stock_agent.cli import _review_priority

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_fixture(root)
            with _patch_project_paths(root):
                _review_priority("config/watchlist.local.json")
                latest = root / "reports" / "workbench" / "priority_queue_latest.json"
                latest_exists = latest.exists()

        self.assertTrue(latest_exists)

    def test_priority_queue_contains_no_trading_actions(self):
        from stock_agent.review_priority import build_priority_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_fixture(root)
            with _patch_project_paths(root):
                queue = build_priority_queue()

        serialized = json.dumps(queue, ensure_ascii=False).lower()
        for forbidden in ["buy", "sell", "order", "rebalance", "trade"]:
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
