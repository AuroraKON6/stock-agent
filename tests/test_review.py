"""Tests for review feedback loop: export-review, review-summary, suggestions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.review import (
    _build_review_record,
    _is_qualifying,
    _load_signals_from_latest_json,
    compute_review_summary,
    export_review,
    write_review_summary,
)


def _make_signal_dict(**overrides):
    base = {
        "signal_id": "test_001",
        "title": "测试信号",
        "priority": "P2",
        "source": "manual",
        "source_type": "news",
        "topics": ["AI算力"],
        "symbols": ["浪潮信息"],
        "needs_review": True,
        "llm_analysis": {"impact_direction": "positive", "confidence": 0.8, "review_questions": ["问题1？"]},
        "reasons": ["正面词: 订单"],
    }
    base.update(overrides)
    return base


def _make_completed_review(signal_id="test_001", was_effective="true", **overrides):
    base = {
        "signal_id": signal_id,
        "title": "测试信号",
        "priority": "P2",
        "source": "manual",
        "source_type": "news",
        "topics": ["AI算力"],
        "symbols": ["浪潮信息"],
        "llm_analysis": {},
        "reasons": [],
        "suggested_review_questions": [],
        "decision_taken": "confirmed",
        "actual_price_move": "+3%",
        "actual_news_followup": "已确认",
        "was_effective": was_effective,
        "false_positive_reason": "",
        "notes": "",
        "reviewed_at": "2026-06-07T10:00:00+08:00",
    }
    base.update(overrides)
    return base


class TestIsQualifying(unittest.TestCase):
    def test_p0_qualifies(self):
        self.assertTrue(_is_qualifying(_make_signal_dict(priority="P0")))

    def test_p1_qualifies(self):
        self.assertTrue(_is_qualifying(_make_signal_dict(priority="P1")))

    def test_p2_qualifies(self):
        self.assertTrue(_is_qualifying(_make_signal_dict(priority="P2")))

    def test_p3_no_extras_does_not_qualify(self):
        s = _make_signal_dict(priority="P3", needs_review=False, llm_analysis={}, reasons=[])
        self.assertFalse(_is_qualifying(s))

    def test_needs_review_qualifies(self):
        s = _make_signal_dict(priority="P3", needs_review=True, llm_analysis={})
        self.assertTrue(_is_qualifying(s))

    def test_llm_analysis_qualifies(self):
        s = _make_signal_dict(priority="P3", needs_review=False, llm_analysis={"key": "val"})
        self.assertTrue(_is_qualifying(s))

    def test_price_anomaly_qualifies(self):
        s = _make_signal_dict(
            priority="P3", needs_review=False, llm_analysis={},
            source_type="price", reasons=["价格显著上涨(4.50%)"],
        )
        self.assertTrue(_is_qualifying(s))

    def test_price_no_anomaly_does_not_qualify(self):
        s = _make_signal_dict(
            priority="P3", needs_review=False, llm_analysis={},
            source_type="price", reasons=["普通原因"],
        )
        self.assertFalse(_is_qualifying(s))


class TestBuildReviewRecord(unittest.TestCase):
    def test_all_fields_present(self):
        record = _build_review_record(_make_signal_dict())
        required = [
            "signal_id", "title", "priority", "source", "source_type",
            "topics", "symbols", "llm_analysis", "reasons",
            "suggested_review_questions", "decision_taken", "actual_price_move",
            "actual_news_followup", "was_effective", "false_positive_reason",
            "notes", "reviewed_at",
        ]
        for field in required:
            self.assertIn(field, record)

    def test_review_questions_from_llm(self):
        record = _build_review_record(_make_signal_dict())
        self.assertEqual(record["suggested_review_questions"], ["问题1？"])

    def test_empty_review_questions(self):
        s = _make_signal_dict(llm_analysis={"confidence": 0.5})
        record = _build_review_record(s)
        self.assertEqual(record["suggested_review_questions"], [])

    def test_default_values(self):
        record = _build_review_record(_make_signal_dict())
        self.assertEqual(record["decision_taken"], "")
        self.assertEqual(record["was_effective"], "unknown")
        self.assertEqual(record["reviewed_at"], "")


class TestExportReview(unittest.TestCase):
    def test_creates_pending_review_jsonl(self):
        signals = [_make_signal_dict()]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = export_review({}, signals)
                self.assertTrue(path.endswith(".jsonl"))
                self.assertTrue(os.path.exists(path))
                content = Path(path).read_text(encoding="utf-8")
                data = json.loads(content)
                self.assertEqual(data["signal_id"], "test_001")
                self.assertEqual(data["was_effective"], "unknown")

    def test_no_qualifying_signals_returns_empty(self):
        signals = [_make_signal_dict(priority="P3", needs_review=False, llm_analysis={}, reasons=[])]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = export_review({}, signals)
                self.assertEqual(path, "")

    def test_reads_from_latest_json_report(self):
        signals_data = [_make_signal_dict()]
        with tempfile.TemporaryDirectory() as tmpdir:
            json_dir = Path(tmpdir) / "reports" / "json"
            json_dir.mkdir(parents=True)
            json_path = json_dir / "20260605T100000_0800.json"
            json_path.write_text(json.dumps({"signals": signals_data}), encoding="utf-8")
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = export_review({})
                self.assertTrue(path.endswith(".jsonl"))

    def test_full_field_output(self):
        signals = [_make_signal_dict()]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = export_review({}, signals)
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertIn("suggested_review_questions", data)
                self.assertIn("decision_taken", data)
                self.assertIn("actual_price_move", data)
                self.assertIn("false_positive_reason", data)

    def test_does_not_export_same_signal_twice(self):
        signals = [_make_signal_dict()]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                first = export_review({}, signals)
                second = export_review({}, signals)
                files = list((Path(tmpdir) / "data" / "review").glob("pending_review_*.jsonl"))
        self.assertTrue(first)
        self.assertEqual(second, "")
        self.assertEqual(len(files), 1)


class TestComputeReviewSummary(unittest.TestCase):
    def _write_completed(self, tmpdir, records):
        review_dir = Path(tmpdir) / "data" / "review"
        review_dir.mkdir(parents=True)
        path = review_dir / "completed_review_20260607T100000_0800.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_counts_correct(self):
        records = [
            _make_completed_review("s1", "true"),
            _make_completed_review("s2", "false"),
            _make_completed_review("s3", "unknown"),
            _make_completed_review("s4", "true"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertEqual(summary["total"], 4)
                self.assertEqual(summary["effective_count"], 2)
                self.assertEqual(summary["ineffective_count"], 1)
                self.assertEqual(summary["unknown_count"], 1)

    def test_effective_rate_by_priority(self):
        records = [
            _make_completed_review("s1", "true", priority="P1"),
            _make_completed_review("s2", "false", priority="P1"),
            _make_completed_review("s3", "true", priority="P2"),
            _make_completed_review("s4", "true", priority="P2"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertAlmostEqual(summary["effective_rate_by_priority"]["P1"]["rate"], 0.5)
                self.assertAlmostEqual(summary["effective_rate_by_priority"]["P2"]["rate"], 1.0)

    def test_effective_rate_by_source(self):
        records = [
            _make_completed_review("s1", "true", source="eastmoney"),
            _make_completed_review("s2", "false", source="rss"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertAlmostEqual(summary["effective_rate_by_source"]["eastmoney"]["rate"], 1.0)
                self.assertAlmostEqual(summary["effective_rate_by_source"]["rss"]["rate"], 0.0)

    def test_effective_rate_by_topic(self):
        records = [
            _make_completed_review("s1", "true", topics=["AI算力"]),
            _make_completed_review("s2", "false", topics=["AI算力"]),
            _make_completed_review("s3", "true", topics=["新能源车"]),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertAlmostEqual(summary["effective_rate_by_topic"]["AI算力"]["rate"], 0.5)
                self.assertAlmostEqual(summary["effective_rate_by_topic"]["新能源车"]["rate"], 1.0)

    def test_no_completed_review_graceful(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertEqual(summary["total"], 0)
                self.assertEqual(summary["effective_count"], 0)

    def test_no_completed_review_still_includes_research_case_status_changes(self):
        from stock_agent.research_cases import record_case_decision

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases_dir = root / "data" / "research_cases"
            cases_dir.mkdir(parents=True, exist_ok=True)
            (cases_dir / "cases.json").write_text(
                json.dumps({
                    "cases": [
                        {
                            "case_id": "case001",
                            "title": "VerificationCo / AI compute / filing:major_contract",
                            "status": "active",
                            "signal_count": 1,
                        }
                    ]
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("stock_agent.review.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                record_case_decision("case001", "needs_evidence", notes="need source")
                summary = compute_review_summary()
                md_path, json_path = write_review_summary(summary)

            md_text = Path(md_path).read_text(encoding="utf-8-sig")

        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["research_case_summary"]["status_counts"]["needs_evidence"], 1)
        self.assertIn("Research Case Status Changes", md_text)
        self.assertIn("needs_evidence", md_text)

    def test_false_positive_reason_aggregation(self):
        records = [
            _make_completed_review("s1", "false", false_positive_reason="信息未证实"),
            _make_completed_review("s2", "false", false_positive_reason="信息未证实"),
            _make_completed_review("s3", "false", false_positive_reason="数据错误"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                fp = summary["common_false_positive_reasons"]
                self.assertEqual(fp[0], ("信息未证实", 2))
                self.assertEqual(fp[1], ("数据错误", 1))

    def test_low_quality_sources_flagged(self):
        records = [_make_completed_review("s{0}".format(i), "false", source="bad_source") for i in range(6)]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertIn("bad_source", summary["low_quality_sources"])


class TestReviewSummaryOutput(unittest.TestCase):
    def test_writes_md_and_json(self):
        summary = {
            "total": 2, "effective_count": 1, "ineffective_count": 0, "unknown_count": 1,
            "effective_rate": 0.5, "effective_rate_by_priority": {}, "effective_rate_by_source": {},
            "effective_rate_by_source_type": {}, "effective_rate_by_topic": {},
            "common_false_positive_reasons": [], "low_quality_sources": [],
            "suggestions": {"keyword_adjustments": [], "threshold_adjustments": []},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                md_path, json_path = write_review_summary(summary)
                self.assertTrue(md_path.endswith(".md"))
                self.assertTrue(json_path.endswith(".json"))
                self.assertTrue(os.path.exists(md_path))
                self.assertTrue(os.path.exists(json_path))

    def test_suggestions_not_modify_config(self):
        summary = {
            "total": 1, "effective_count": 0, "ineffective_count": 1, "unknown_count": 0,
            "effective_rate": 0.0, "effective_rate_by_priority": {}, "effective_rate_by_source": {},
            "effective_rate_by_source_type": {}, "effective_rate_by_topic": {},
            "common_false_positive_reasons": [], "low_quality_sources": [],
            "suggestions": {"keyword_adjustments": ["test"], "threshold_adjustments": ["test"]},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_file = config_dir / "watchlist.local.json"
            config_file.write_text("{}")
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                write_review_summary(summary)
                self.assertEqual(config_file.read_text(), "{}")

    def test_md_has_all_sections(self):
        summary = {
            "total": 1, "effective_count": 1, "ineffective_count": 0, "unknown_count": 0,
            "effective_rate": 1.0,
            "effective_rate_by_priority": {"P1": {"count": 1, "effective": 1, "rate": 1.0}},
            "effective_rate_by_source": {"manual": {"count": 1, "effective": 1, "rate": 1.0}},
            "effective_rate_by_source_type": {"news": {"count": 1, "effective": 1, "rate": 1.0}},
            "effective_rate_by_topic": {"AI算力": {"count": 1, "effective": 1, "rate": 1.0}},
            "common_false_positive_reasons": [], "low_quality_sources": [],
            "suggestions": {"keyword_adjustments": [], "threshold_adjustments": []},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                md_path, _ = write_review_summary(summary)
                content = Path(md_path).read_text(encoding="utf-8-sig")
                self.assertIn("复盘汇总报告", content)
                self.assertIn("按优先级有效率", content)
                self.assertIn("按数据源有效率", content)
                self.assertIn("按主题有效率", content)


class TestSuggestions(unittest.TestCase):
    def _write_completed(self, tmpdir, records):
        review_dir = Path(tmpdir) / "data" / "review"
        review_dir.mkdir(parents=True)
        path = review_dir / "completed_review_20260607T100000_0800.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_low_p1_effective_rate_suggests_threshold(self):
        records = [_make_completed_review("s{0}".format(i), "false", priority="P1") for i in range(6)]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                th = summary["suggestions"]["threshold_adjustments"]
                self.assertTrue(any("min_priority_for_alert" in s for s in th))

    def test_high_p2_effective_rate_suggests_lowering(self):
        records = [_make_completed_review("s{0}".format(i), "true", priority="P2") for i in range(6)]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                th = summary["suggestions"]["threshold_adjustments"]
                self.assertTrue(any("P2" in s and "降低" in s for s in th))

    def test_no_suggestions_when_data_insufficient(self):
        records = [_make_completed_review("s1", "false", priority="P1")]
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_completed(tmpdir, records)
            with patch("stock_agent.review.project_path", side_effect=lambda p: Path(tmpdir) / p):
                summary = compute_review_summary()
                self.assertEqual(summary["suggestions"]["threshold_adjustments"], [])


class TestReportEnhancement(unittest.TestCase):
    def test_pending_review_shown_in_report(self):
        from stock_agent.reports import _render_markdown
        with tempfile.TemporaryDirectory() as tmpdir:
            review_dir = Path(tmpdir) / "data" / "review"
            review_dir.mkdir(parents=True)
            (review_dir / "pending_review_20260607T100000_0800.jsonl").write_text("{}")
            with patch("stock_agent.reports.project_path", side_effect=lambda p: Path(tmpdir) / p):
                config = {"review": {"fields": []}}
                md = _render_markdown(config, "2026-06-07", [], [], [], {})
                self.assertIn("复盘反馈", md)
                self.assertIn("待复核文件", md)

    def test_review_summary_shown_in_report(self):
        from stock_agent.reports import _render_markdown
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_dir = Path(tmpdir) / "reports" / "review"
            summary_dir.mkdir(parents=True)
            (summary_dir / "review_summary_20260607T100000_0800.md").write_text("# test")
            with patch("stock_agent.reports.project_path", side_effect=lambda p: Path(tmpdir) / p):
                config = {"review": {"fields": []}}
                md = _render_markdown(config, "2026-06-07", [], [], [], {})
                self.assertIn("复盘反馈", md)
                self.assertIn("最新复盘汇总", md)

    def test_no_review_files_no_section(self):
        from stock_agent.reports import _render_markdown
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.reports.project_path", side_effect=lambda p: Path(tmpdir) / p):
                config = {"review": {"fields": []}}
                md = _render_markdown(config, "2026-06-07", [], [], [], {})
                self.assertNotIn("复盘反馈", md)


if __name__ == "__main__":
    unittest.main()
