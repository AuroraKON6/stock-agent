import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import now_iso


def _latest_payload():
    return {
        "generated_at": "2026-06-19T21:00:00+08:00",
        "source_degraded_event_count": 1,
        "source_degraded_events": [
            {"source_id": "eastmoney_filing", "severity": "FAIL", "detail": "timeout"}
        ],
        "remediation_queue_count": 4,
        "remediation_queue": [
            {
                "task_id": "remediation:signal:s001:001",
                "task_type": "fill_missing_url",
                "gap_type": "missing_url",
                "gap": "missing_url",
                "source_id": "eastmoney_filing",
                "signal_id": "s001",
                "status": "open",
                "created_at": "2026-06-19T20:00:00+08:00",
                "allowed_actions": ["attach_evidence_url", "verify_source_url", "defer_signal"],
            },
            {
                "task_id": "remediation:signal:s001:002",
                "task_type": "fill_missing_fields",
                "gap_type": "missing_fields",
                "gap": "missing_fields",
                "source_id": "eastmoney_filing",
                "signal_id": "s001",
                "status": "open",
                "created_at": "2026-06-19T20:10:00+08:00",
                "allowed_actions": ["attach_structured_fields", "request_more_data", "defer_signal"],
            },
            {
                "task_id": "remediation:source:eastmoney:001",
                "task_type": "repair_source",
                "gap_type": "source_degraded",
                "gap": "source_degraded",
                "source_id": "eastmoney",
                "signal_id": "",
                "status": "open",
                "created_at": "2026-06-19T20:20:00+08:00",
                "allowed_actions": ["retry_source", "enable_fallback_source", "open_source_health_report", "defer_signal"],
            },
            {
                "task_id": "remediation:signal:s002:001",
                "task_type": "verify_official_filing",
                "gap_type": "official_filing",
                "gap": "official_filing_url",
                "source_id": "eastmoney_filing",
                "signal_id": "s002",
                "status": "open",
                "created_at": "2026-06-19T20:30:00+08:00",
                "allowed_actions": ["verify_official_filing", "attach_filing_url", "confirm_signal", "reject_signal", "defer_signal"],
            },
        ],
        "remediation_gap_type_counts": {
            "missing_url": 1,
            "missing_fields": 1,
            "source_degraded": 1,
            "official_filing": 1,
        },
        "remediation_status_counts": {"open": 4},
        "source_failure_counts": {"eastmoney_filing": 1},
        "open_count": 4,
        "resolved_count": 0,
    }


def _write_latest(root: Path):
    d = root / "reports" / "remediation"
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(json.dumps(_latest_payload(), ensure_ascii=False), encoding="utf-8")
    (d / "latest.md").write_text("# Source Remediation Queue\n", encoding="utf-8")


class RemediationDecisionCoreTests(unittest.TestCase):
    def test_decision_records_survive_regenerated_task_ids(self):
        from stock_agent.source_remediation import build_remediation_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "data" / "remediation"
            log_dir.mkdir(parents=True)
            record = {
                "record_id": "remediation:legacy",
                "task_id": "remediation:signal:s001:legacy",
                "task_type": "fill_missing_url",
                "gap_type": "missing_url",
                "source_id": "eastmoney_filing",
                "signal_id": "s001",
                "action": "defer_remediation",
                "status": "deferred",
                "actor": "analyst",
                "notes": "wait for official source",
                "recorded_at": "2026-06-19T22:00:00+08:00",
            }
            (log_dir / "decision_log_20260619.jsonl").write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            signal = {
                "signal_id": "s001",
                "source": "eastmoney_filing",
                "source_type": "filing",
                "url": "https://example.test/notice",
                "evidence_gap_tasks": [{"gap": "missing_url", "status": "open"}],
            }
            with patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p):
                summary = build_remediation_summary([signal], {"source_degraded_events": []}, {})

        task = summary["remediation_queue"][0]
        self.assertEqual(task["task_id"], "remediation:signal:s001:001")
        self.assertEqual(task["status"], "deferred")
        self.assertEqual(task["last_action"], "defer_remediation")
        self.assertEqual(summary["remediation_status_counts"]["deferred"], 1)

    def test_attach_evidence_url_writes_log_and_resolves_task(self):
        from stock_agent.source_remediation import attach_evidence_url, load_remediation_decision_records

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p):
                record = attach_evidence_url(
                    "remediation:signal:s001:001",
                    "https://example.test/official.pdf",
                    actor="analyst",
                    notes="official announcement found",
                )
                records = load_remediation_decision_records()
                latest = json.loads((root / "reports" / "remediation" / "latest.json").read_text(encoding="utf-8"))

        self.assertEqual(record["action"], "attach_evidence_url")
        self.assertEqual(records[0]["task_id"], "remediation:signal:s001:001")
        task = latest["remediation_queue"][0]
        self.assertEqual(task["status"], "resolved")
        self.assertEqual(task["evidence_url"], "https://example.test/official.pdf")
        self.assertEqual(task["resolved_by"], "analyst")
        self.assertTrue(task["resolved_at"])
        serialized = json.dumps(task, ensure_ascii=False).lower()
        self.assertNotIn("buy", serialized)
        self.assertNotIn("sell", serialized)

    def test_attach_fields_reject_and_defer_update_task_statuses(self):
        from stock_agent.source_remediation import (
            attach_structured_fields,
            defer_remediation_task,
            reject_remediation_task,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p):
                attach_structured_fields(
                    "remediation:signal:s001:002",
                    {"announcement_type": "major_contract", "report_period": "2026Q2"},
                    actor="analyst",
                )
                reject_remediation_task(
                    "remediation:source:eastmoney:001",
                    reason="provider outage is already covered by fallback",
                    actor="analyst",
                )
                defer_remediation_task(
                    "remediation:signal:s002:001",
                    reason="wait for exchange disclosure",
                    actor="analyst",
                )
                latest = json.loads((root / "reports" / "remediation" / "latest.json").read_text(encoding="utf-8"))

        by_id = {task["task_id"]: task for task in latest["remediation_queue"]}
        self.assertEqual(by_id["remediation:signal:s001:002"]["status"], "resolved")
        self.assertEqual(by_id["remediation:signal:s001:002"]["structured_fields"]["announcement_type"], "major_contract")
        self.assertEqual(by_id["remediation:source:eastmoney:001"]["status"], "rejected")
        self.assertEqual(by_id["remediation:signal:s002:001"]["status"], "deferred")
        self.assertEqual(latest["remediation_status_counts"]["resolved"], 1)
        self.assertEqual(latest["remediation_status_counts"]["rejected"], 1)
        self.assertEqual(latest["remediation_status_counts"]["deferred"], 1)


class RemediationCliTests(unittest.TestCase):
    def test_cli_shortcuts_call_remediation_actions(self):
        from stock_agent.cli import (
            _attach_evidence_url,
            _attach_structured_fields,
            _defer_remediation,
            _reject_remediation,
            _resolve_remediation,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest(root)
            with patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p):
                _attach_evidence_url("remediation:signal:s001:001", "https://example.test/a.pdf", "user", "url ok")
                _attach_structured_fields("remediation:signal:s001:002", ["announcement_type=major_contract"], "user", "")
                _reject_remediation("remediation:source:eastmoney:001", "covered by fallback", "user")
                _defer_remediation("remediation:signal:s002:001", "waiting", "user")
                _resolve_remediation("remediation:signal:s002:001", "manual done", "user")
                latest = json.loads((root / "reports" / "remediation" / "latest.json").read_text(encoding="utf-8"))

        status_by_id = {task["task_id"]: task["status"] for task in latest["remediation_queue"]}
        self.assertEqual(status_by_id["remediation:signal:s001:001"], "resolved")
        self.assertEqual(status_by_id["remediation:signal:s001:002"], "resolved")
        self.assertEqual(status_by_id["remediation:source:eastmoney:001"], "rejected")
        self.assertEqual(status_by_id["remediation:signal:s002:001"], "resolved")


class RemediationSurfaceTests(unittest.TestCase):
    def test_dashboard_groups_task_statuses_and_recent_records(self):
        from stock_agent.dashboard import _render_dashboard_html

        payload = _latest_payload()
        payload["remediation_queue"][0]["status"] = "resolved"
        payload["remediation_queue"][1]["status"] = "rejected"
        payload["remediation_queue"][2]["status"] = "deferred"
        payload["remediation_queue"][3]["status"] = "open"
        html = _render_dashboard_html({
            "generated_at": now_iso(),
            "signals": [],
            "stats": {
                "source_health": {"overall_status": "WARN", "sources": []},
                "remediation_queue": payload["remediation_queue"],
                "remediation_decision_records": [
                    {"task_id": "remediation:signal:s001:001", "action": "attach_evidence_url", "actor": "analyst"}
                ],
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

        self.assertIn("Source Remediation Queue", html)
        self.assertIn("Open", html)
        self.assertIn("Resolved", html)
        self.assertIn("Rejected", html)
        self.assertIn("Deferred", html)
        self.assertIn("Recent Remediation Records", html)
        self.assertIn("attach_evidence_url", html)

    def test_review_summary_tracks_completion_rate_resolution_time_and_common_types(self):
        from stock_agent.review import compute_review_summary, write_review_summary

        payload = _latest_payload()
        payload["remediation_queue"][0]["status"] = "resolved"
        payload["remediation_queue"][0]["resolved_at"] = "2026-06-19T22:00:00+08:00"
        payload["remediation_queue"][1]["status"] = "rejected"
        payload["remediation_queue"][2]["status"] = "deferred"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports" / "json").mkdir(parents=True)
            (root / "data" / "review").mkdir(parents=True)
            (root / "reports" / "json" / "latest.json").write_text(json.dumps({
                "signals": [],
                "stats": {
                    "source_degraded_events": payload["source_degraded_events"],
                    "remediation_queue": payload["remediation_queue"],
                    "remediation_gap_type_counts": payload["remediation_gap_type_counts"],
                },
            }, ensure_ascii=False), encoding="utf-8")
            with patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                summary = compute_review_summary()
                md_path, _json_path = write_review_summary(summary)
                md = Path(md_path).read_text(encoding="utf-8-sig")

        rem = summary["source_remediation_summary"]
        self.assertEqual(rem["resolved_count"], 1)
        self.assertEqual(rem["rejected_count"], 1)
        self.assertEqual(rem["deferred_count"], 1)
        self.assertGreater(rem["completion_rate"], 0)
        self.assertGreater(rem["average_resolution_hours"], 0)
        self.assertIn("fill_missing_url", rem["common_task_types"])
        self.assertIn("completion_rate", md)
        self.assertIn("average_resolution_hours", md)

    def test_daily_report_shows_recent_remediation_changes(self):
        from stock_agent.reports import write_outputs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for rel in ["reports/daily", "reports/json", "reports/alerts"]:
                (root / rel).mkdir(parents=True, exist_ok=True)
            with patch("stock_agent.reports.project_path", side_effect=lambda p: root / p):
                report_path, _json_path, _alert_path, _alerts = write_outputs(
                    [],
                    {"report": {"include_low_priority": True}, "priority_rules": {}},
                    {
                        "source_health": {"overall_status": "WARN", "sources": []},
                        "remediation_queue": _latest_payload()["remediation_queue"],
                        "remediation_decision_records": [
                            {"task_id": "remediation:signal:s001:001", "action": "attach_evidence_url", "status": "resolved"}
                        ],
                    },
                )
                report = Path(report_path).read_text(encoding="utf-8-sig")

        self.assertIn("remediation_changes", report)
        self.assertIn("attach_evidence_url", report)
        self.assertIn("resolved", report)

    def test_dashboard_context_and_review_summary_read_remediation_latest_after_cli_update(self):
        from stock_agent.dashboard import _load_dashboard_context
        from stock_agent.review import compute_review_summary

        payload = _latest_payload()
        payload["remediation_queue"][0]["status"] = "deferred"
        payload["remediation_status_counts"] = {"open": 3, "deferred": 1}
        payload["open_count"] = 3
        payload["deferred_count"] = 1
        payload["remediation_decision_records"] = [
            {"task_id": "remediation:signal:s001:001", "action": "defer_remediation", "status": "deferred"}
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports" / "remediation").mkdir(parents=True)
            (root / "reports" / "json").mkdir(parents=True)
            (root / "data" / "review").mkdir(parents=True)
            (root / "reports" / "remediation" / "latest.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: root / p):
                ctx = _load_dashboard_context()
            with patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                summary = compute_review_summary()

        self.assertEqual(ctx["stats"]["deferred_count"], 1)
        self.assertEqual(ctx["stats"]["remediation_decision_records"][0]["action"], "defer_remediation")
        self.assertEqual(summary["source_remediation_summary"]["deferred_count"], 1)
        self.assertEqual(summary["source_remediation_summary"]["recent_records"][0]["action"], "defer_remediation")


if __name__ == "__main__":
    unittest.main()
