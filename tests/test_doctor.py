"""Tests for doctor health check: status levels, config checks, API key safety."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from stock_agent.doctor import (
    run_doctor,
    write_doctor_report,
    _check_config,
    _check_llm,
    _check_llm_connectivity,
    _check_llm_governance,
    _check_dashboard,
    _check_notifications,
    _check_workspace_inbox,
    _check_review_data,
    _check_research_operations,
    _check_research_runtime,
    _check_research_workpapers,
    _check_scheduled_tasks,
)


class TestResearchRuntimeHealth(unittest.TestCase):
    def test_safe_runtime_policy_is_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data" / "research_runtime"
            path.mkdir(parents=True)
            (path / "runtime.json").write_text(json.dumps({
                "policy": {
                    "auto_trade": False,
                    "auto_publish": False,
                    "external_publication_enabled": False,
                    "human_approval_required": True,
                    "role_separation_required": True,
                },
                "cases": [{"stage": "analysis", "approvals": {}}],
            }), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_research_runtime({"research_runtime": {"enabled": True}})
        self.assertEqual(status, "OK")
        self.assertIn("cases=1", detail)

    def test_unsafe_runtime_policy_is_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data" / "research_runtime"
            path.mkdir(parents=True)
            (path / "runtime.json").write_text(json.dumps({
                "policy": {
                    "auto_trade": True,
                    "auto_publish": True,
                    "external_publication_enabled": True,
                    "human_approval_required": False,
                    "role_separation_required": False,
                },
                "cases": [],
            }), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_research_runtime({"research_runtime": {"enabled": True}})
        self.assertEqual(status, "FAIL")
        self.assertIn("Unsafe research runtime policy", detail)


class TestResearchWorkpaperHealth(unittest.TestCase):
    def test_disabled_workpapers_are_explicitly_ok(self):
        status, detail, note = _check_research_workpapers({})
        self.assertEqual(status, "OK")
        self.assertIn("disabled", detail)
        self.assertEqual(note, "")

    def test_missing_workpaper_ledger_is_warn(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p
        ):
            status, detail, note = _check_research_workpapers({
                "research_workpapers": {"enabled": True, "enforce": True}
            })
        self.assertEqual(status, "WARN")
        self.assertIn("not found", detail)
        self.assertIn("protected approvals remain blocked", note)

    def test_technical_failure_is_fail_and_business_block_is_warn(self):
        config = {"research_workpapers": {"enabled": True, "enforce": True}}
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "data" / "research_workpapers" / "events.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("{}\n", encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p), patch(
                "stock_agent.doctor.verify_research_workpapers",
                return_value={
                    "technical_status": "FAIL", "business_status": "BLOCKED",
                    "integrity_status": "FAIL", "workpaper_count": 1, "event_count": 1,
                },
            ):
                failed = _check_research_workpapers(config)
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p), patch(
                "stock_agent.doctor.verify_research_workpapers",
                return_value={
                    "technical_status": "READY", "business_status": "BLOCKED",
                    "integrity_status": "OK", "workpaper_count": 1, "event_count": 1,
                },
            ):
                blocked = _check_research_workpapers(config)
        self.assertEqual(failed[0], "FAIL")
        self.assertEqual(blocked[0], "WARN")
        self.assertIn("business=BLOCKED", blocked[1])


class TestResearchOperationsHealth(unittest.TestCase):
    def _write_snapshot(self, root, payload):
        path = Path(root) / "reports" / "research_operations"
        path.mkdir(parents=True)
        (path / "latest.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_mandatory_humans_blocks_business_readiness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_snapshot(tmpdir, {
                "enforce_roster": True,
                "assignment_mode": "suggest_only",
                "roster": {
                    "valid": True,
                    "errors": [],
                    "missing_mandatory_human_capabilities": ["lead_analyst", "compliance_officer"],
                },
                "summary": {"case_count": 4, "unstaffed_count": 4, "overdue_count": 2},
                "cases": [],
            })
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, note = _check_research_operations({
                    "research_operations": {"enabled": True, "enforce_roster": True}
                })
        self.assertEqual(status, "WARN")
        self.assertIn("Business readiness BLOCKED", detail)
        self.assertIn("compliance_officer", detail)
        self.assertIn("service agents cannot approve", note)

    def test_invalid_roster_is_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_snapshot(tmpdir, {
                "enforce_roster": True,
                "assignment_mode": "suggest_only",
                "roster": {"valid": False, "errors": ["duplicate actor_id"]},
                "summary": {},
                "cases": [],
            })
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_research_operations({
                    "research_operations": {"enabled": True, "enforce_roster": True}
                })
        self.assertEqual(status, "FAIL")
        self.assertIn("duplicate actor_id", detail)

    def test_automatic_assignment_is_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_snapshot(tmpdir, {
                "enforce_roster": True,
                "assignment_mode": "suggest_only",
                "roster": {"valid": True, "errors": [], "missing_mandatory_human_capabilities": []},
                "summary": {},
                "cases": [{"automatic_assignment_executed": True}],
            })
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_research_operations({
                    "research_operations": {"enabled": True, "enforce_roster": True}
                })
        self.assertEqual(status, "FAIL")
        self.assertIn("automatic assignment", detail)

    def test_fully_staffed_operations_are_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_snapshot(tmpdir, {
                "enforce_roster": True,
                "assignment_mode": "suggest_only",
                "roster": {"valid": True, "errors": [], "missing_mandatory_human_capabilities": []},
                "summary": {
                    "case_count": 2,
                    "unstaffed_count": 0,
                    "unstaffed_high_priority_count": 0,
                    "at_risk_count": 0,
                    "overdue_count": 0,
                    "restricted_count": 0,
                },
                "cases": [{"automatic_assignment_executed": False}],
            })
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_research_operations({
                    "research_operations": {"enabled": True, "enforce_roster": True}
                })
        self.assertEqual(status, "OK")
        self.assertIn("Business readiness ACTIVE", detail)


class TestScheduledTaskResults(unittest.TestCase):
    def test_nonzero_last_result_is_visible(self):
        rows = [
            {"TaskName": "StockAgent_DailyResearch", "State": "Ready", "LastTaskResult": 3221225786},
            {"TaskName": "StockAgent_DailyDashboard", "State": "Ready", "LastTaskResult": 0},
            {"TaskName": "StockAgent_WeeklyReview", "State": "Ready", "LastTaskResult": 267011},
        ]
        proc = MagicMock(returncode=0, stdout=json.dumps(rows))
        with patch("stock_agent.doctor.subprocess.run", return_value=proc):
            results = _check_scheduled_tasks()
        by_name = {item[1].split(":", 1)[0].split()[-1]: item for item in results}
        self.assertEqual(by_name["StockAgent_DailyResearch"][0], "WARN")
        self.assertIn("0xC000013A", by_name["StockAgent_DailyResearch"][1])
        self.assertEqual(by_name["StockAgent_DailyDashboard"][0], "OK")
        self.assertEqual(by_name["StockAgent_WeeklyReview"][0], "WARN")


class TestConfigCheck(unittest.TestCase):
    def test_config_missing_returns_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, note, config = _check_config("config/nonexistent.json")
                self.assertEqual(status, "FAIL")
                self.assertIsNone(config)

    def test_config_invalid_json_returns_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / "config"
            cfg_dir.mkdir()
            cfg_file = cfg_dir / "bad.json"
            cfg_file.write_text("not valid json {{{", encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, note, config = _check_config("config/bad.json")
                self.assertEqual(status, "FAIL")
                self.assertIsNone(config)

    def test_config_valid_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / "config"
            cfg_dir.mkdir()
            cfg_file = cfg_dir / "good.json"
            cfg_file.write_text(json.dumps({"topics": []}), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, note, config = _check_config("config/good.json")
                self.assertEqual(status, "OK")
                self.assertIsNotNone(config)


class TestLLMCheck(unittest.TestCase):
    def test_no_key_returns_warn(self):
        with patch.dict(os.environ, {}, clear=True):
            env = {"LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}
            with patch.dict(os.environ, env, clear=False):
                # Remove the keys if they exist
                os.environ.pop("LLM_API_KEY", None)
                os.environ.pop("DEEPSEEK_API_KEY", None)
                results = _check_llm({"llm_agent": {"enabled": True}})
                statuses = [r[0] for r in results]
                details = [r[1] for r in results]
                self.assertIn("WARN", statuses)
                # Ensure no key is leaked
                for d in details:
                    self.assertNotIn("sk-", d)
                    self.assertNotIn("key=", d.lower())

    def test_with_key_returns_ok(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "sk-test-secret-key-12345"}):
            results = _check_llm({"llm_agent": {"enabled": True}})
            statuses = [r[0] for r in results]
            details = [r[1] for r in results]
            self.assertIn("OK", statuses)
            # Key must not appear in output
            for d in details:
                self.assertNotIn("sk-test-secret-key-12345", d)

    def test_key_not_in_detail_text(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "super-secret-value"}):
            results = _check_llm({"llm_agent": {"enabled": False}})
            for _, detail, _ in results:
                self.assertNotIn("super-secret-value", detail)


class TestLLMGovernanceHealth(unittest.TestCase):
    def _config(self, **overrides):
        raw = {
            "enabled": True,
            "enforce": True,
            "external_provider": True,
            "allowed_external_classifications": ["public"],
            "block_secrets": True,
            "block_personal_data": True,
            "block_nonpublic_information": True,
            "block_prompt_injection": True,
            "require_evidence_refs": True,
            "audit_enabled": True,
        }
        raw.update(overrides)
        return {"llm_governance": raw}

    def _write_snapshot(self, root, summary, **overrides):
        path = Path(root) / "reports" / "llm_governance"
        path.mkdir(parents=True)
        payload = {
            "enforced": True,
            "allowed_external_classifications": ["public"],
            "summary": summary,
            "audit_paths": ["data/llm_governance/audit/audit.jsonl"] if summary.get("decision_count") else [],
        }
        payload.update(overrides)
        (path / "latest.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_unsafe_policy_is_fail(self):
        status, detail, _ = _check_llm_governance(self._config(
            allowed_external_classifications=["public", "restricted"]
        ))
        self.assertEqual(status, "FAIL")
        self.assertIn("Unsafe LLM governance policy", detail)

    def test_denied_egress_is_visible_as_warn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_snapshot(tmpdir, {
                "decision_count": 2,
                "approved_count": 1,
                "denied_count": 1,
                "output_blocked_count": 0,
                "prompt_injection_blocked_count": 1,
                "pending_exception_count": 1,
            })
            with patch("stock_agent.doctor.project_path", side_effect=lambda value: Path(tmpdir) / value):
                status, detail, note = _check_llm_governance(self._config())
        self.assertEqual(status, "WARN")
        self.assertIn("denied=1", detail)
        self.assertIn("no automatic exception", note)

    def test_clean_enforced_snapshot_is_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_snapshot(tmpdir, {
                "decision_count": 1,
                "approved_count": 1,
                "denied_count": 0,
                "output_blocked_count": 0,
                "prompt_injection_blocked_count": 0,
                "pending_exception_count": 0,
            })
            with patch("stock_agent.doctor.project_path", side_effect=lambda value: Path(tmpdir) / value):
                status, detail, _ = _check_llm_governance(self._config())
        self.assertEqual(status, "OK")
        self.assertIn("approved=1", detail)

    @patch.dict(os.environ, {"LLM_API_KEY": "generic-key", "DEEPSEEK_API_KEY": "deepseek-key"}, clear=False)
    def test_connectivity_prefers_deepseek_key_for_deepseek_base_url(self):
        captured = {}

        def fake_probe(options):
            captured.update(options)
            return {"ok": True, "base_url": options["base_url"]}

        with patch("stock_agent.doctor._probe_llm_connectivity", side_effect=fake_probe):
            status, _detail, _note = _check_llm_connectivity({
                "llm_agent": {
                    "enabled": True,
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-pro",
                }
            })

        self.assertEqual(status, "OK")
        self.assertEqual(captured["api_key"], "deepseek-key")

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "bad-deepseek", "LLM_FALLBACK_API_KEY": "good-fallback"}, clear=False)
    def test_connectivity_falls_back_to_next_provider_when_primary_fails(self):
        attempts = []

        def fake_probe(options):
            attempts.append(options)
            if options["base_url"] == "https://api.deepseek.com/v1":
                return {
                    "ok": False,
                    "base_url": options["base_url"],
                    "error": "HTTP Error 401: Unauthorized",
                    "diagnosis": "credential_or_token_error",
                    "repair_guide": [],
                }
            return {"ok": True, "base_url": options["base_url"]}

        with patch("stock_agent.doctor._probe_llm_connectivity", side_effect=fake_probe):
            status, detail, _note = _check_llm_connectivity({
                "llm_agent": {
                    "enabled": True,
                    "providers": [
                        {
                            "name": "deepseek_primary",
                            "base_url": "https://api.deepseek.com/v1",
                            "model": "deepseek-v4-pro",
                            "api_key_env": "DEEPSEEK_API_KEY",
                        },
                        {
                            "name": "fallback_openai_compatible",
                            "base_url": "https://fallback.example/v1",
                            "model": "fallback-model",
                            "api_key_env": "LLM_FALLBACK_API_KEY",
                        },
                    ],
                }
            })

        self.assertEqual(status, "OK")
        self.assertIn("fallback_openai_compatible", detail)
        self.assertEqual([item["api_key"] for item in attempts], ["bad-deepseek", "good-fallback"])


class TestDashboardCheck(unittest.TestCase):
    def test_dashboard_exists_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dash_dir = Path(tmpdir) / "reports" / "dashboard"
            dash_dir.mkdir(parents=True)
            (dash_dir / "latest.html").write_text("<html></html>", encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_dashboard()
                self.assertEqual(status, "OK")

    def test_dashboard_missing_warn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_dashboard()
                self.assertEqual(status, "WARN")


class TestNotificationsCheck(unittest.TestCase):
    def test_notifications_exists_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            n_dir = Path(tmpdir) / "reports" / "notifications"
            n_dir.mkdir(parents=True)
            (n_dir / "latest.md").write_text("# test", encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_notifications()
                self.assertEqual(status, "OK")

    def test_notifications_missing_warn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_notifications()
                self.assertEqual(status, "WARN")


class TestWorkspaceInboxCheck(unittest.TestCase):
    def test_workspace_inbox_exists_ok_with_object_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir) / "reports" / "workspace"
            workspace_dir.mkdir(parents=True)
            (workspace_dir / "latest.md").write_text("# Workspace Inbox", encoding="utf-8")
            (workspace_dir / "latest.json").write_text(
                json.dumps({"object_counts": {"signals": 2, "evidence_items": 5, "comments": 3}}),
                encoding="utf-8",
            )
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_workspace_inbox()
                self.assertEqual(status, "OK")
                self.assertIn("signals=2", detail)
                self.assertIn("evidence=5", detail)

    def test_workspace_inbox_missing_warn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                status, detail, _ = _check_workspace_inbox()
                self.assertEqual(status, "WARN")
                self.assertIn("Workspace Inbox not found", detail)


class TestReviewDataCheck(unittest.TestCase):
    def test_no_review_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                results = _check_review_data()
                self.assertEqual(len(results), 2)
                for status, _, _ in results:
                    self.assertEqual(status, "OK")

    def test_pending_review_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_dir = Path(tmpdir) / "data" / "review"
            review_dir.mkdir(parents=True)
            p = review_dir / "pending_review_20260605.jsonl"
            lines = [
                json.dumps({"signal_id": "s1"}),
                json.dumps({"signal_id": "s2"}),
                json.dumps({"signal_id": "s3"}),
            ]
            p.write_text("\n".join(lines), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                results = _check_review_data()
                pending_detail = results[0][1]
                self.assertIn("3", pending_detail)

    def test_completed_review_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_dir = Path(tmpdir) / "data" / "review"
            review_dir.mkdir(parents=True)
            p = review_dir / "completed_review_20260605.jsonl"
            lines = [json.dumps({"signal_id": "s1"})]
            p.write_text("\n".join(lines), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                results = _check_review_data()
                completed_detail = results[1][1]
                self.assertIn("1", completed_detail)

    def test_review_backlog_deduplicates_and_subtracts_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_dir = Path(tmpdir) / "data" / "review"
            review_dir.mkdir(parents=True)
            (review_dir / "pending_review_1.jsonl").write_text("\n".join([
                json.dumps({"signal_id": "s1"}),
                json.dumps({"signal_id": "s1"}),
                json.dumps({"signal_id": "s2"}),
            ]), encoding="utf-8")
            (review_dir / "completed_review_1.jsonl").write_text(
                json.dumps({"signal_id": "s1"}), encoding="utf-8"
            )
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                results = _check_review_data({"review": {"backlog_warn_count": 2}})
        self.assertEqual(results[0][0], "OK")
        self.assertIn("rows=3", results[0][1])
        self.assertIn("unique=2", results[0][1])
        self.assertIn("outstanding=1", results[0][1])

    def test_review_backlog_warns_without_any_completed_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_dir = Path(tmpdir) / "data" / "review"
            review_dir.mkdir(parents=True)
            (review_dir / "pending_review_1.jsonl").write_text(
                json.dumps({"signal_id": "s1"}), encoding="utf-8"
            )
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                results = _check_review_data({"review": {"backlog_warn_count": 20, "warn_if_no_completed": True}})
        self.assertEqual(results[0][0], "WARN")
        self.assertIn("no completed review", results[0][2])


class TestOverallStatus(unittest.TestCase):
    def test_all_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup: valid config, dashboard, notifications
            cfg_dir = Path(tmpdir) / "config"
            cfg_dir.mkdir()
            cfg = {
                "sources": {
                    "local_jsonl": [{"name": "test", "path": "data/test.jsonl", "enabled": False}],
                    "eastmoney": {"enabled": False},
                    "rss": [],
                },
                "llm_agent": {"enabled": False},
            }
            (cfg_dir / "ok.json").write_text(json.dumps(cfg), encoding="utf-8")
            dash_dir = Path(tmpdir) / "reports" / "dashboard"
            dash_dir.mkdir(parents=True)
            (dash_dir / "latest.html").write_text("<html>", encoding="utf-8")
            n_dir = Path(tmpdir) / "reports" / "notifications"
            n_dir.mkdir(parents=True)
            (n_dir / "latest.md").write_text("# test", encoding="utf-8")
            w_dir = Path(tmpdir) / "reports" / "workspace"
            w_dir.mkdir(parents=True)
            (w_dir / "latest.md").write_text("# Workspace Inbox", encoding="utf-8")
            (w_dir / "latest.json").write_text(
                json.dumps({"object_counts": {"signals": 1, "evidence_items": 2, "comments": 1}}),
                encoding="utf-8",
            )

            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                with patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=False):
                    with patch("stock_agent.doctor.shutil.which", return_value="/usr/bin/openclaw"):
                        with patch("stock_agent.doctor.subprocess.run") as mock_run:
                            mock_proc = MagicMock()
                            mock_proc.stdout = ""
                            mock_run.return_value = mock_proc
                            with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                                result = run_doctor("config/ok.json")
                                # Should be WARN (openclaw registry missing) but not FAIL
                                self.assertIn(result["overall_status"], ("OK", "WARN"))
                                self.assertTrue(any(c["name"] == "workspace_inbox" for c in result["checks"]))

    def test_config_fail_overrides_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                result = run_doctor("config/missing.json")
                self.assertEqual(result["overall_status"], "FAIL")

    def test_warn_from_missing_dashboard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / "config"
            cfg_dir.mkdir()
            cfg = {
                "sources": {"local_jsonl": [], "eastmoney": {"enabled": False}, "rss": []},
                "llm_agent": {"enabled": False},
            }
            (cfg_dir / "ok.json").write_text(json.dumps(cfg), encoding="utf-8")
            # No dashboard, no notifications → WARN
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                with patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=False):
                    with patch("stock_agent.doctor.shutil.which", return_value=None):
                        with patch("stock_agent.doctor.subprocess.run") as mock_run:
                            mock_proc = MagicMock()
                            mock_proc.stdout = ""
                            mock_run.return_value = mock_proc
                            with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                                result = run_doctor("config/ok.json")
                                self.assertEqual(result["overall_status"], "WARN")


class TestWriteDoctorReport(unittest.TestCase):
    def test_creates_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / "config"
            cfg_dir.mkdir()
            cfg = {
                "sources": {"local_jsonl": [], "eastmoney": {"enabled": False}, "rss": []},
                "llm_agent": {"enabled": False},
            }
            (cfg_dir / "ok.json").write_text(json.dumps(cfg), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                with patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=False):
                    with patch("stock_agent.doctor.shutil.which", return_value=None):
                        with patch("stock_agent.doctor.subprocess.run") as mock_run:
                            mock_proc = MagicMock()
                            mock_proc.stdout = ""
                            mock_run.return_value = mock_proc
                            with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                                path, result = write_doctor_report("config/ok.json")
                                self.assertTrue(os.path.exists(path))
                                health_dir = Path(tmpdir) / "reports" / "health"
                                self.assertTrue((health_dir / "latest.md").exists())
                                self.assertTrue((health_dir / "latest.json").exists())
                                ts_files = list(health_dir.glob("doctor_*.md"))
                                self.assertEqual(len(ts_files), 1)


class TestOpenClawRegistry(unittest.TestCase):
    def test_parses_list_structure(self):
        """Registry uses agents.list[] with id field, not agents[id]."""
        reg = {
            "agents": {
                "defaults": {"model": {"primary": "deepseek/deepseek-v4-flash"}},
                "list": [
                    {"id": "qwen-orchestrator", "name": "调度总管"},
                    {"id": "stock_agent_coordinator", "name": "投研调度协调器"},
                    {"id": "stock_agent_source_collector", "name": "数据源采集器"},
                    {"id": "stock_agent_signal_analyst", "name": "信号研判分析师"},
                ],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            oc_dir = Path(tmpdir) / ".openclaw"
            oc_dir.mkdir()
            (oc_dir / "openclaw.json").write_text(json.dumps(reg), encoding="utf-8")
            with patch("stock_agent.doctor.shutil.which", return_value="/usr/bin/openclaw"):
                with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                    from stock_agent.doctor import _check_openclaw
                    results = _check_openclaw()
                    details = [r[1] for r in results]
                    statuses = [r[0] for r in results]
                    # Should find stock_agent_ agents
                    self.assertIn("OK", statuses)
                    self.assertTrue(any("stock_agent_coordinator" in d for d in details))
                    self.assertTrue(any("stock_agent_source_collector" in d for d in details))

    def test_empty_list_warns(self):
        reg = {"agents": {"defaults": {}, "list": []}}
        with tempfile.TemporaryDirectory() as tmpdir:
            oc_dir = Path(tmpdir) / ".openclaw"
            oc_dir.mkdir()
            (oc_dir / "openclaw.json").write_text(json.dumps(reg), encoding="utf-8")
            with patch("stock_agent.doctor.shutil.which", return_value="/usr/bin/openclaw"):
                with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                    from stock_agent.doctor import _check_openclaw
                    results = _check_openclaw()
                    statuses = [r[0] for r in results]
                    self.assertIn("WARN", statuses)

    def test_empty_plugin_allow_list_warns(self):
        reg = {
            "agents": {"defaults": {}, "list": [{"id": "stock_agent_coordinator"}]},
            "plugins": {"allow": []},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            oc_dir = Path(tmpdir) / ".openclaw"
            oc_dir.mkdir()
            (oc_dir / "openclaw.json").write_text(json.dumps(reg), encoding="utf-8")
            with patch("stock_agent.doctor.shutil.which", return_value="/usr/bin/openclaw"):
                with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                    from stock_agent.doctor import _check_openclaw
                    results = _check_openclaw()
                    self.assertTrue(any(status == "WARN" and "plugins.allow" in detail for status, detail, _ in results))

    def test_explicit_plugin_allow_list_passes(self):
        reg = {
            "agents": {"defaults": {}, "list": [{"id": "stock_agent_coordinator"}]},
            "plugins": {"allow": ["deepseek"]},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            oc_dir = Path(tmpdir) / ".openclaw"
            oc_dir.mkdir()
            (oc_dir / "openclaw.json").write_text(json.dumps(reg), encoding="utf-8")
            with patch("stock_agent.doctor.shutil.which", return_value="/usr/bin/openclaw"):
                with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                    from stock_agent.doctor import _check_openclaw
                    results = _check_openclaw()
                    self.assertTrue(any(status == "OK" and "plugins.allow" in detail for status, detail, _ in results))


class TestAPIKeySafety(unittest.TestCase):
    def test_no_api_key_in_any_output(self):
        secret = "sk-super-secret-key-that-must-not-leak"
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / "config"
            cfg_dir.mkdir()
            cfg = {
                "sources": {"local_jsonl": [], "eastmoney": {"enabled": False}, "rss": []},
                "llm_agent": {"enabled": True},
            }
            (cfg_dir / "ok.json").write_text(json.dumps(cfg), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: Path(tmpdir) / p):
                with patch.dict(os.environ, {"LLM_API_KEY": secret}):
                    with patch("stock_agent.doctor.shutil.which", return_value=None):
                        with patch("stock_agent.doctor.subprocess.run") as mock_run:
                            mock_proc = MagicMock()
                            mock_proc.stdout = ""
                            mock_run.return_value = mock_proc
                            with patch("stock_agent.doctor.Path.home", return_value=Path(tmpdir)):
                                result = run_doctor("config/ok.json")
                                # Check all check details
                                for c in result["checks"]:
                                    self.assertNotIn(secret, c["detail"],
                                        "API key leaked in check: {0}".format(c["name"]))
                                # Check rendered markdown
                                from stock_agent.doctor import _render_doctor_markdown
                                md = _render_doctor_markdown(result)
                                self.assertNotIn(secret, md)


if __name__ == "__main__":
    unittest.main()
