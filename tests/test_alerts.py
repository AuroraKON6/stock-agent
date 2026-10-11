"""Tests for alerts/notification generation: trigger conditions, output format, edge cases."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.alerts import (
    generate_notifications,
    _check_triggers,
    _load_latest_json_report,
    _load_latest_alerts,
    _load_latest_pending_review,
    _load_latest_actions,
    _load_research_runtime,
)


def _make_signal(signal_id="s001", title="test", priority="P2", source_type="news",
                 llm_analysis=None, reasons=None):
    return {
        "signal_id": signal_id,
        "title": title,
        "priority": priority,
        "source": "test",
        "source_type": source_type,
        "topics": [],
        "symbols": [],
        "llm_analysis": llm_analysis,
        "reasons": reasons or [],
    }


def _write_json_report(tmpdir, signals, stats=None):
    d = Path(tmpdir) / "reports" / "json"
    d.mkdir(parents=True, exist_ok=True)
    data = {"generated_at": "2026-06-05T10:00:00", "signals": signals, "stats": stats or {}, "alerts": []}
    p = d / "20260605T100000.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_alerts(tmpdir, alerts):
    d = Path(tmpdir) / "reports" / "alerts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "20260605T100000.jsonl"
    lines = [json.dumps(a, ensure_ascii=False) for a in alerts]
    p.write_text("\n".join(lines), encoding="utf-8")


def _write_pending_review(tmpdir, records):
    d = Path(tmpdir) / "data" / "review"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "pending_review_20260605T100000.jsonl"
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    p.write_text("\n".join(lines), encoding="utf-8")


def _write_actions(tmpdir, actions):
    d = Path(tmpdir) / "reports" / "actions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "actions_20260605T100000.jsonl"
    lines = [json.dumps(a, ensure_ascii=False) for a in actions]
    p.write_text("\n".join(lines), encoding="utf-8")


def _write_research_runtime(tmpdir, cases):
    d = Path(tmpdir) / "reports" / "research_runtime"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": {
            "case_count": len(cases),
            "pending_human_count": sum(
                1 for item in cases
                if item.get("stage") in {"quality_review", "compliance_review", "human_approval"}
            ),
        },
        "runtime": {"cases": cases},
    }
    (d / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestCheckTriggers(unittest.TestCase):
    def test_no_triggers_empty_data(self):
        triggers = _check_triggers([], [], [], [])
        self.assertEqual(triggers, [])

    def test_p0_alert_trigger(self):
        alerts = [{"signal_id": "a1", "priority": "P0", "title": "t"}]
        triggers = _check_triggers([], alerts, [], [])
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0][0], "high_priority_alerts")

    def test_p1_alert_trigger(self):
        alerts = [{"signal_id": "a1", "priority": "P1", "title": "t"}]
        triggers = _check_triggers([], alerts, [], [])
        self.assertEqual(len(triggers), 1)

    def test_p2_no_alert_trigger(self):
        alerts = [{"signal_id": "a1", "priority": "P2", "title": "t"}]
        triggers = _check_triggers([], alerts, [], [])
        self.assertEqual(len(triggers), 0)

    def test_llm_human_decision_trigger(self):
        signals = [_make_signal(llm_analysis={
            "confidence": 0.85,
            "human_decision_needed": True,
            "impact_direction": "positive",
        })]
        triggers = _check_triggers(signals, [], [], [])
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0][0], "llm_human_decision")

    def test_llm_low_confidence_no_trigger(self):
        signals = [_make_signal(llm_analysis={
            "confidence": 0.5,
            "human_decision_needed": True,
        })]
        triggers = _check_triggers(signals, [], [], [])
        self.assertEqual(len(triggers), 0)

    def test_llm_no_human_decision_no_trigger(self):
        signals = [_make_signal(llm_analysis={
            "confidence": 0.9,
            "human_decision_needed": False,
        })]
        triggers = _check_triggers(signals, [], [], [])
        self.assertEqual(len(triggers), 0)

    def test_source_error_trigger(self):
        signals = [_make_signal(source_type="source_error")]
        triggers = _check_triggers(signals, [], [], [])
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0][0], "source_errors")

    def test_pending_review_trigger(self):
        pending = [{"signal_id": "r1", "title": "review"}]
        triggers = _check_triggers([], [], pending, [])
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0][0], "pending_review")

    def test_multiple_triggers(self):
        signals = [
            _make_signal(source_type="source_error"),
            _make_signal(signal_id="s2", llm_analysis={
                "confidence": 0.8, "human_decision_needed": True,
            }),
        ]
        alerts = [{"signal_id": "a1", "priority": "P0", "title": "t"}]
        pending = [{"signal_id": "r1", "title": "review"}]
        triggers = _check_triggers(signals, alerts, pending, [])
        names = [t[0] for t in triggers]
        self.assertIn("high_priority_alerts", names)
        self.assertIn("llm_human_decision", names)
        self.assertIn("source_errors", names)
        self.assertIn("pending_review", names)

    def test_research_runtime_human_gate_trigger(self):
        runtime = {
            "runtime": {
                "cases": [{"case_id": "case-a", "stage": "quality_review", "approvals": {}}]
            }
        }
        triggers = _check_triggers([], [], [], [], research_runtime=runtime)
        self.assertIn("research_runtime_human_gate", [item[0] for item in triggers])

    def test_research_runtime_compliance_block_trigger(self):
        runtime = {
            "runtime": {
                "cases": [{
                    "case_id": "case-a",
                    "stage": "compliance_review",
                    "approvals": {"compliance_review": {"status": "blocked"}},
                }]
            }
        }
        triggers = _check_triggers([], [], [], [], research_runtime=runtime)
        names = [item[0] for item in triggers]
        self.assertIn("research_runtime_human_gate", names)
        self.assertIn("research_runtime_compliance_block", names)


class TestGenerateNotifications(unittest.TestCase):
    def test_no_triggers_generates_ok_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                self.assertIsNotNone(path)
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("正常", content)
                self.assertIn("无异常触发条件", content)

    def test_with_high_priority_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [_make_signal()])
            _write_alerts(tmpdir, [{"signal_id": "a1", "priority": "P0", "title": "紧急预警"}])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("P0/P1 预警", content)
                self.assertIn("紧急预警", content)

    def test_with_llm_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            signals = [_make_signal(llm_analysis={
                "confidence": 0.9,
                "human_decision_needed": True,
                "impact_direction": "positive",
            })]
            _write_json_report(tmpdir, signals)
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("高置信度 LLM", content)
                self.assertIn("人工决策", content)

    def test_with_source_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [_make_signal(source_type="source_error", title="数据源故障")])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("数据源异常", content)

    def test_with_pending_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [])
            _write_pending_review(tmpdir, [{"signal_id": "r1", "title": "待复核", "priority": "P2"}])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("待复核信号", content)
                self.assertIn("待复核", content)
                self.assertIn("[P2]", content)

    def test_with_research_runtime_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [])
            _write_research_runtime(tmpdir, [{
                "case_id": "case-a",
                "top_priority": "P1",
                "title": "重大合同",
                "stage": "human_approval",
                "approvals": {"compliance_review": {"status": "approved"}},
            }])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("机构投研门禁待办", content)
                self.assertIn("case-a", content)

    def test_latest_md_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path1 = generate_notifications()
                time.sleep(1.1)
                path2 = generate_notifications()
                self.assertEqual(path1, path2)
                notify_dir = Path(tmpdir) / "reports" / "notifications"
                ts_files = list(notify_dir.glob("notification_*.md"))
                self.assertEqual(len(ts_files), 2)

    def test_statistics_section_always_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [_make_signal()])
            _write_alerts(tmpdir, [{"signal_id": "a1", "priority": "P0", "title": "t"}])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("信号总数:", content)
                self.assertIn("预警总数:", content)


class TestLoadFunctions(unittest.TestCase):
    def test_load_json_report_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                result = _load_latest_json_report()
                self.assertEqual(result["signals"], [])

    def test_load_alerts_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                result = _load_latest_alerts()
                self.assertEqual(result, [])

    def test_load_pending_review_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                result = _load_latest_pending_review()
                self.assertEqual(result, [])

    def test_load_actions_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                result = _load_latest_actions()
                self.assertEqual(result, [])

    def test_load_research_runtime_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                result = _load_research_runtime()
                self.assertEqual(result["runtime"]["cases"], [])


class TestNotificationFormat(unittest.TestCase):
    def test_valid_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, [])
            with patch("stock_agent.alerts.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = generate_notifications()
                content = Path(path).read_text(encoding="utf-8")
                self.assertTrue(content.startswith("# A股投研系统通知"))
                self.assertIn("生成时间:", content)


if __name__ == "__main__":
    unittest.main()
