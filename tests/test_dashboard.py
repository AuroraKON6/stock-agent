"""Tests for dashboard generation: data loading, KPI, HTML rendering, empty states."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.dashboard import (
    generate_dashboard,
    _dashboard_css,
    _load_dashboard_context,
    _load_latest_json_report,
    _load_latest_alerts,
    _load_current_research_cases,
    _load_latest_pending_review,
    _load_latest_review_summary,
    _render_dashboard_html,
    _section_research_workpapers,
    _section_value_validation,
)


def _make_signal_dict(signal_id="s001", title="测试信号", priority="P2", source="manual",
                      source_type="news", topics=None, symbols=None, llm_analysis=None,
                      reasons=None, needs_review=False, payload=None):
    return {
        "signal_id": signal_id,
        "title": title,
        "priority": priority,
        "source": source,
        "source_type": source_type,
        "topics": topics or [],
        "symbols": symbols or [],
        "llm_analysis": llm_analysis or {},
        "reasons": reasons or [],
        "needs_review": needs_review,
        "payload": payload or {},
    }


def _make_json_report(signals=None, stats=None):
    return {
        "generated_at": "2026-06-05T10:00:00+08:00",
        "signals": signals or [],
        "stats": stats or {},
        "team": {},
        "alerts": [],
    }


def _write_json_report(tmpdir, filename, data):
    json_dir = Path(tmpdir) / "reports" / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    path = json_dir / filename
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _write_alerts(tmpdir, filename, alerts):
    alerts_dir = Path(tmpdir) / "reports" / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    path = alerts_dir / filename
    lines = [json.dumps(a, ensure_ascii=False) for a in alerts]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_pending_review(tmpdir, filename, records):
    review_dir = Path(tmpdir) / "data" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / filename
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_review_summary(tmpdir, filename, data):
    summary_dir = Path(tmpdir) / "reports" / "review"
    summary_dir.mkdir(parents=True, exist_ok=True)
    path = summary_dir / filename
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class DashboardCurrentCaseAuthorityTests(unittest.TestCase):
    def test_context_prefers_current_case_store_over_stale_report_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = _make_json_report()
            report["research_cases"] = [{"case_id": "stale-uat", "title": "UAT"}]
            _write_json_report(tmpdir, "20260811T090000_0800.json", report)
            case_path = Path(tmpdir) / "data" / "research_cases" / "cases.json"
            case_path.parent.mkdir(parents=True)
            case_path.write_text(json.dumps({
                "cases": [{"case_id": "prod-current", "title": "Production"}]
            }), encoding="utf-8")
            with patch("stock_agent.dashboard.project_path", side_effect=lambda value: Path(tmpdir) / value), patch(
                "stock_agent.dashboard.build_priority_queue", return_value={}
            ), patch("stock_agent.dashboard.load_latest_architecture_state", return_value={}):
                context = _load_dashboard_context()

        self.assertEqual(context["research_cases"][0]["case_id"], "prod-current")

    def test_current_case_loader_returns_empty_authoritative_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data" / "research_cases" / "cases.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"cases": []}', encoding="utf-8")
            with patch("stock_agent.dashboard.project_path", side_effect=lambda value: Path(tmpdir) / value):
                self.assertEqual(_load_current_research_cases(), [])


class TestLoadDashboardContext(unittest.TestCase):
    def test_no_files_empty_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                self.assertEqual(ctx["signals"], [])
                self.assertEqual(ctx["alerts_list"], [])
                self.assertEqual(ctx["pending_review"], [])
                self.assertIsNone(ctx["review_summary"])

    def test_with_json_report(self):
        signals = [_make_signal_dict()]
        report = _make_json_report(signals=signals)
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, "20260605T100000_0800.json", report)
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                self.assertEqual(len(ctx["signals"]), 1)
                self.assertEqual(ctx["signals"][0]["signal_id"], "s001")

    def test_with_alerts(self):
        alerts = [{"signal_id": "a1", "title": "alert1", "priority": "P1"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_alerts(tmpdir, "20260605T100000_0800.jsonl", alerts)
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                self.assertEqual(len(ctx["alerts_list"]), 1)

    def test_with_pending_review(self):
        records = [{"signal_id": "r1", "title": "review1", "was_effective": "unknown"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_pending_review(tmpdir, "pending_review_20260605T100000_0800.jsonl", records)
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                self.assertEqual(len(ctx["pending_review"]), 1)

    def test_with_review_summary(self):
        summary = {"total": 5, "effective_count": 3, "effective_rate": 0.6}
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_review_summary(tmpdir, "review_summary_20260607T100000_0800.json", summary)
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                self.assertIsNotNone(ctx["review_summary"])
                self.assertEqual(ctx["review_summary"]["total"], 5)


class TestKPICards(unittest.TestCase):
    def test_kpi_in_html(self):
        signals = [
            _make_signal_dict(priority="P0"),
            _make_signal_dict(signal_id="s2", priority="P1"),
            _make_signal_dict(signal_id="s3", priority="P2"),
            _make_signal_dict(signal_id="s4", priority="P3"),
            _make_signal_dict(signal_id="s5", priority="P2", source_type="source_error"),
        ]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": signals,
            "stats": {},
            "alerts_list": [{"signal_id": "a1"}],
            "pending_review": [{"signal_id": "r1"}],
            "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("P0", html)
        self.assertIn("P1", html)
        self.assertIn("P2", html)
        self.assertIn("P3", html)
        self.assertIn("今日信号", html)
        self.assertIn("预警", html)
        self.assertIn("LLM研判", html)
        self.assertIn("待复盘", html)
        self.assertIn("源异常", html)


class TestValueValidationSection(unittest.TestCase):
    def test_value_metrics_and_human_decision_boundary_are_visible(self):
        html = _section_value_validation({
            "study_id": "a-share-value-v1",
            "operating_status": "VALUE_TEST",
            "integrity_status": "OK",
            "value_gate_ready": False,
            "recommended_decision": "INSUFFICIENT_EVIDENCE",
            "decision": {},
            "cohort": {"case_count": 20},
            "aggregate": {
                "qualifying_case_count": 12,
                "blind_complete_count": 12,
                "median_efficiency_improvement": 0.24,
                "weighted_efficiency_improvement": 0.21,
                "quality_delta": 1.5,
                "estimated_cost_cny": 18.5,
                "unauthorized_egress_count": 0,
                "protocol_deviation_case_count": 1,
            },
            "gates": [{
                "gate": "paired_case_completeness",
                "status": "FAIL",
                "detail": "blind_complete=12; qualifying=12; cohort=20",
            }],
            "next_actions": ["Complete independent blinded scoring."],
            "safety_boundary": "No automatic approval, publication, or trading.",
        })
        self.assertIn("G0.5 价值验证", html)
        self.assertIn("24.0%", html)
        self.assertIn("INSUFFICIENT_EVIDENCE", html)
        self.assertIn("NOT_RECORDED", html)
        self.assertIn("未授权外发", html)
        self.assertIn("No automatic approval", html)

    def test_missing_projection_is_read_only_empty_state(self):
        html = _section_value_validation({})
        self.assertIn("value-validation-status", html)
        self.assertIn("不会创建研究或调用模型", html)


class TestPriceTable(unittest.TestCase):
    def test_price_signal_in_table(self):
        signals = [_make_signal_dict(
            source_type="price",
            payload={"latest": 150.5, "pct_change": 4.5, "main_net_inflow": 200000000},
            reasons=["价格显著上涨(4.50%)"],
        )]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": signals, "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("行情快照", html)
        self.assertIn("150.50", html)
        self.assertIn("+4.50%", html)
        self.assertIn("2.00亿", html)

    def test_non_price_excluded(self):
        signals = [_make_signal_dict(source_type="news")]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": signals, "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("暂无行情数据", html)


class TestHighPrioritySignals(unittest.TestCase):
    def test_p0_p1_p2_included(self):
        signals = [
            _make_signal_dict(priority="P0", title="P0信号"),
            _make_signal_dict(signal_id="s2", priority="P1", title="P1信号"),
            _make_signal_dict(signal_id="s3", priority="P2", title="P2信号"),
        ]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": signals, "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("P0信号", html)
        self.assertIn("P1信号", html)
        self.assertIn("P2信号", html)

    def test_p3_excluded(self):
        signals = [_make_signal_dict(priority="P3", title="P3信号")]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": signals, "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("暂无高优先级信号", html)


class TestLLMSection(unittest.TestCase):
    def test_llm_analysis_shown(self):
        llm = {
            "impact_direction": "positive",
            "confidence": 0.85,
            "horizon": "1-5d",
            "key_thesis": "重大利好",
            "risk_points": ["风险1"],
            "review_questions": ["问题1？"],
            "human_decision_needed": True,
        }
        signals = [_make_signal_dict(llm_analysis=llm)]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": signals, "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("重大利好", html)
        self.assertIn("positive", html)
        self.assertIn("85%", html)
        self.assertIn("风险1", html)
        self.assertIn("问题1？", html)

    def test_no_llm_shows_empty(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [], "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("暂无 LLM 研判数据", html)


class TestResearchWorkpaperSection(unittest.TestCase):
    def test_workpaper_chain_and_business_blockers_are_visible(self):
        html = _section_research_workpapers({
            "integrity_status": "OK",
            "summary": {
                "event_count": 3,
                "workpaper_count": 1,
                "approval_eligible_count": 0,
                "business_status": "BLOCKED",
                "production_archive_ready": False,
            },
            "workpapers": [{
                "case_id": "case-v27-dashboard",
                "version": 3,
                "case": {"priority": "P1"},
                "workflow": {"stage": "challenge"},
                "source_records": [{"primary_source_eligible": True}],
                "claims": [{"claim_type": "source_statement"}],
                "validation": {
                    "status": "WARN",
                    "errors": ["independent human challenge is required"],
                },
            }],
        })
        self.assertIn("Research Workpapers", html)
        self.assertIn("OK", html)
        self.assertIn("BLOCKED", html)
        self.assertIn("case-v27-dashbo", html)
        self.assertIn("independent human challenge", html)
        self.assertIn("审计影子账本", html)

    def test_empty_workpaper_section_has_operator_command(self):
        html = _section_research_workpapers({})
        self.assertIn("research-workpaper-sync", html)

    def test_mobile_table_scroll_does_not_expand_root_document(self):
        css = _dashboard_css()
        self.assertIn("overflow-x: clip", css)
        self.assertIn("contain: inline-size", css)
        self.assertIn(".table-scroll", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn(".card li", css)
        self.assertIn("-webkit-overflow-scrolling: touch", css)


class TestResearchGitSection(unittest.TestCase):
    def test_research_git_handoff_shown(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [],
            "stats": {
                "research_git_enabled": True,
                "research_git_auto_staged_count": 2,
                "research_git_draft_count": 5,
                "research_git_pending_review_count": 3,
                "research_git_status_counts": {"draft": 2, "committed": 3},
                "research_git_draft_ids": ["draft_a", "draft_b"],
                "research_git_summary_latest_path": "E:/stock agent/reports/research_git/latest.md",
            },
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("Research-as-Git", html)
        self.assertIn("auto_staged", html)
        self.assertIn("draft_a", html)
        self.assertIn("latest.md", html)


class TestWorkspaceInboxSection(unittest.TestCase):
    def test_workspace_inbox_shown_from_stats(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [],
            "stats": {
                "workspace_inbox": {
                    "object_counts": {
                        "signals": 8,
                        "evidence_items": 27,
                        "comments": 25,
                        "research_drafts": 1,
                    },
                    "workspace_latest_path": "E:/stock agent/reports/workspace/latest.md",
                }
            },
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("Workspace Inbox", html)
        self.assertIn("evidence=27", html)
        self.assertIn("comments=25", html)
        self.assertIn("latest.md", html)


class TestPendingReviewQueue(unittest.TestCase):
    def test_pending_review_shown(self):
        pending = [{
            "signal_id": "abc123",
            "title": "待复核信号",
            "priority": "P2",
            "suggested_review_questions": ["问题A？"],
            "was_effective": "unknown",
        }]
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [], "stats": {}, "alerts_list": [],
            "pending_review": pending, "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("待复核信号", html)
        self.assertIn("问题A？", html)

    def test_empty_pending(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [], "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("暂无待复核项", html)


class TestReviewStats(unittest.TestCase):
    def test_review_summary_shown(self):
        summary = {
            "total": 10,
            "effective_count": 7,
            "effective_rate": 0.7,
            "effective_rate_by_priority": {"P1": {"count": 5, "effective": 3, "rate": 0.6}},
            "effective_rate_by_topic": {"AI算力": {"count": 4, "effective": 3, "rate": 0.75}},
            "common_false_positive_reasons": [("信息未证实", 2)],
            "suggestions": {"keyword_adjustments": ["建议1"], "threshold_adjustments": []},
        }
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [], "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": summary,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("70%", html)
        self.assertIn("信息未证实", html)
        self.assertIn("建议1", html)

    def test_no_review_summary_shows_empty(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [], "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("暂无已完成复盘数据", html)


class TestTeamSection(unittest.TestCase):
    def test_team_context_loaded_from_latest_json(self):
        team = {
            "supervisor_brief": {"headline": "今日有 1 条高优先级信号需要上司确认。"},
            "messages": [{"from": "risk_manager", "to": "strategy_reviewer", "body": "需要反证"}],
            "guardrails": ["no_auto_trading"],
        }
        report = _make_json_report(signals=[], stats={"team_enabled": True})
        report["team"] = team
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_json_report(tmpdir, "20260605T100000_0800.json", report)
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                self.assertEqual(ctx["team"]["supervisor_brief"]["headline"], team["supervisor_brief"]["headline"])

    def test_team_section_rendered(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [],
            "stats": {"team_enabled": True, "team_role_count": 6, "team_assignment_count": 4, "team_message_count": 1},
            "team": {
                "supervisor_brief": {
                    "headline": "今日有 1 条高优先级信号需要上司确认。",
                    "today_focus": ["高优先级信号复核"],
                    "next_actions": ["人工确认来源"],
                },
                "messages": [{"from": "risk_manager", "to": "strategy_reviewer", "body": "需要反证"}],
                "guardrails": ["no_auto_trading"],
            },
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("投研团队", html)
        self.assertIn("今日有 1 条高优先级信号需要上司确认", html)
        self.assertIn("risk_manager", html)
        self.assertIn("no_auto_trading", html)


class TestHTMLGeneration(unittest.TestCase):
    def test_valid_html(self):
        ctx = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [], "stats": {}, "alerts_list": [],
            "pending_review": [], "review_summary": None,
        }
        html = _render_dashboard_html(ctx)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)
        self.assertIn("A股智能投研看板", html)

    def test_latest_overwritten_timestamp_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path1 = generate_dashboard()
                self.assertTrue(os.path.exists(path1))
                content1 = Path(path1).read_text(encoding="utf-8-sig")
                dash_dir = Path(tmpdir) / "reports" / "dashboard"
                ts_files_before = list(dash_dir.glob("dashboard_*.html"))
                self.assertEqual(len(ts_files_before), 1)

                time.sleep(1.1)
                path2 = generate_dashboard()
                self.assertEqual(path1, path2)
                content2 = Path(path2).read_text(encoding="utf-8-sig")
                ts_files_after = list(dash_dir.glob("dashboard_*.html"))
                self.assertEqual(len(ts_files_after), 2)


class TestNoData(unittest.TestCase):
    def test_empty_state_no_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: Path(tmpdir) / p):
                ctx = _load_dashboard_context()
                html = _render_dashboard_html(ctx)
                self.assertIn("<!DOCTYPE html>", html)
                self.assertIn("暂无行情数据", html)
                self.assertIn("暂无高优先级信号", html)
                self.assertIn("暂无 LLM 研判数据", html)
                self.assertIn("暂无待复核项", html)
                self.assertIn("暂无已完成复盘数据", html)

    def test_missing_directories_no_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "empty"
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: empty / p):
                ctx = _load_dashboard_context()
                self.assertEqual(ctx["signals"], [])
                self.assertIsNone(ctx["review_summary"])


if __name__ == "__main__":
    unittest.main()
