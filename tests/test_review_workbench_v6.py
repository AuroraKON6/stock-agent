import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def _latest_payload():
    return {
        "generated_at": "2026-06-20T09:00:00+08:00",
        "source_degraded_event_count": 1,
        "source_degraded_events": [
            {"source_id": "eastmoney", "severity": "FAIL", "detail": "timeout"}
        ],
        "remediation_queue_count": 4,
        "remediation_queue": [
            {
                "task_id": "remediation:source:eastmoney:001",
                "task_type": "repair_source",
                "gap_type": "source_degraded",
                "gap": "source_degraded",
                "source_id": "eastmoney",
                "signal_id": "",
                "status": "open",
                "created_at": "2026-06-20T08:30:00+08:00",
                "allowed_actions": ["retry_source", "enable_fallback_source", "open_source_health_report", "defer_signal"],
            },
            {
                "task_id": "remediation:signal:s001:001",
                "task_type": "fill_missing_url",
                "gap_type": "missing_url",
                "gap": "missing_url",
                "source_id": "eastmoney_filing",
                "signal_id": "s001",
                "status": "open",
                "created_at": "2026-06-20T08:31:00+08:00",
                "allowed_actions": ["attach_evidence_url", "verify_source_url", "defer_signal"],
            },
            {
                "task_id": "remediation:signal:s002:001",
                "task_type": "fill_missing_fields",
                "gap_type": "missing_fields",
                "gap": "missing_fields",
                "source_id": "market_cache",
                "signal_id": "s002",
                "status": "resolved",
                "created_at": "2026-06-20T08:32:00+08:00",
                "resolved_at": "2026-06-20T08:50:00+08:00",
                "allowed_actions": ["attach_structured_fields", "request_more_data", "defer_signal"],
            },
            {
                "task_id": "remediation:signal:s003:001",
                "task_type": "verify_official_filing",
                "gap_type": "official_filing",
                "gap": "official_filing_url",
                "source_id": "eastmoney_filing",
                "signal_id": "s003",
                "status": "deferred",
                "created_at": "2026-06-20T08:33:00+08:00",
                "allowed_actions": ["verify_official_filing", "attach_filing_url", "confirm_signal", "reject_signal", "defer_signal"],
            },
        ],
        "remediation_gap_type_counts": {
            "source_degraded": 1,
            "missing_url": 1,
            "missing_fields": 1,
            "official_filing": 1,
        },
        "remediation_status_counts": {"open": 2, "resolved": 1, "deferred": 1},
        "source_failure_counts": {"eastmoney": 1},
        "open_count": 2,
        "resolved_count": 1,
        "rejected_count": 0,
        "deferred_count": 1,
        "remediation_decision_records": [],
    }


def _write_latest(root: Path) -> None:
    out = root / "reports" / "remediation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(_latest_payload(), ensure_ascii=False), encoding="utf-8")
    (out / "latest.md").write_text("# Source Remediation Queue\n", encoding="utf-8")


def _patch_project_paths(root: Path):
    stack = ExitStack()
    for name in [
        "stock_agent.source_remediation.project_path",
        "stock_agent.dashboard.project_path",
        "stock_agent.review.project_path",
        "stock_agent.review_workbench.project_path",
    ]:
        stack.enter_context(patch(name, side_effect=lambda p, r=root: r / p))
    return stack


class ReviewWorkbenchV6Tests(unittest.TestCase):
    def test_workbench_state_filters_tasks_by_status(self):
        from stock_agent.review_workbench import load_workbench_state, render_workbench_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with _patch_project_paths(root):
                state = load_workbench_state(status="open")
                html = render_workbench_html(status="open")

        self.assertEqual(state["status"], "open")
        self.assertEqual([task["task_id"] for task in state["tasks"]], [
            "remediation:source:eastmoney:001",
            "remediation:signal:s001:001",
        ])
        self.assertEqual(state["status_counts"]["open"], 2)
        self.assertIn("?status=open", html)
        self.assertIn("?status=resolved", html)
        self.assertIn("remediation:signal:s001:001", html)
        self.assertNotIn("remediation:signal:s002:001</a>", html)

    def test_task_detail_renders_forms_for_manual_actions(self):
        from stock_agent.review_workbench import render_workbench_html

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with _patch_project_paths(root):
                html = render_workbench_html(status="all", task_id="remediation:signal:s001:001")

        self.assertIn("Task Detail", html)
        self.assertIn("remediation:signal:s001:001", html)
        self.assertIn('name="evidence_url"', html)
        self.assertIn('name="structured_fields"', html)
        self.assertIn('value="attach_evidence_url"', html)
        self.assertIn('value="attach_structured_fields"', html)
        self.assertIn('value="resolve_remediation"', html)
        self.assertIn('value="reject_remediation"', html)
        self.assertIn('value="defer_remediation"', html)

    def test_dashboard_exposes_local_workbench_entry(self):
        from stock_agent.dashboard import _render_dashboard_html

        payload = _latest_payload()
        html = _render_dashboard_html({
            "generated_at": "2026-06-20T09:10:00+08:00",
            "signals": [],
            "stats": {
                "source_health": {"overall_status": "WARN", "sources": []},
                "remediation_queue": payload["remediation_queue"],
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

        self.assertIn("review-workbench", html)
        self.assertIn("http://127.0.0.1:8767", html)

    def test_submit_evidence_url_writes_log_and_refreshes_outputs(self):
        from stock_agent.review_workbench import submit_workbench_action

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with _patch_project_paths(root):
                result = submit_workbench_action({
                    "task_id": "remediation:signal:s001:001",
                    "action": "attach_evidence_url",
                    "evidence_url": "https://example.test/notice.pdf",
                    "actor": "analyst",
                    "notes": "official source checked",
                })
                latest = json.loads((root / "reports" / "remediation" / "latest.json").read_text(encoding="utf-8"))
                decision_logs = list((root / "data" / "remediation").glob("decision_log_*.jsonl"))
                dashboard_exists = (root / "reports" / "dashboard" / "latest.html").exists()
                result_paths_exist = (
                    Path(result["dashboard_path"]).exists(),
                    Path(result["review_summary_path"]).exists(),
                    Path(result["review_summary_json_path"]).exists(),
                )

        task = next(item for item in latest["remediation_queue"] if item["task_id"] == "remediation:signal:s001:001")
        self.assertEqual(task["status"], "resolved")
        self.assertEqual(task["evidence_url"], "https://example.test/notice.pdf")
        self.assertTrue(decision_logs)
        self.assertTrue(dashboard_exists)
        self.assertTrue(all(result_paths_exist))

    def test_submit_structured_fields_resolve_reject_and_defer(self):
        from stock_agent.review_workbench import submit_workbench_action

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with _patch_project_paths(root):
                submit_workbench_action({
                    "task_id": "remediation:signal:s002:001",
                    "action": "attach_structured_fields",
                    "structured_fields": "announcement_type=contract\nreport_period=2026Q2",
                    "actor": "analyst",
                })
                submit_workbench_action({
                    "task_id": "remediation:source:eastmoney:001",
                    "action": "defer_remediation",
                    "notes": "wait for provider recovery",
                    "actor": "analyst",
                })
                submit_workbench_action({
                    "task_id": "remediation:signal:s003:001",
                    "action": "reject_remediation",
                    "notes": "not actionable",
                    "actor": "analyst",
                })
                submit_workbench_action({
                    "task_id": "remediation:source:eastmoney:001",
                    "action": "resolve_remediation",
                    "notes": "fallback verified",
                    "actor": "analyst",
                })
                latest = json.loads((root / "reports" / "remediation" / "latest.json").read_text(encoding="utf-8"))

        by_id = {task["task_id"]: task for task in latest["remediation_queue"]}
        self.assertEqual(by_id["remediation:signal:s002:001"]["status"], "resolved")
        self.assertEqual(by_id["remediation:signal:s002:001"]["structured_fields"]["announcement_type"], "contract")
        self.assertEqual(by_id["remediation:signal:s003:001"]["status"], "rejected")
        self.assertEqual(by_id["remediation:source:eastmoney:001"]["status"], "resolved")

    def test_workbench_rejects_forbidden_trading_actions(self):
        from stock_agent.review_workbench import submit_workbench_action

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with _patch_project_paths(root):
                with self.assertRaises(ValueError):
                    submit_workbench_action({
                        "task_id": "remediation:signal:s001:001",
                        "action": "place_order",
                        "actor": "analyst",
                    })


if __name__ == "__main__":
    unittest.main()
