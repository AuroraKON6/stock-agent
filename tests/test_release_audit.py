from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.release_audit import (
    build_release_audit,
    render_release_audit_markdown,
    write_release_audit,
)
from stock_agent.release_controls import build_release_candidate_identity


NOW = datetime(2026, 8, 31, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))


class ReleaseAuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._make_ready_files()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _path(self, relative: str) -> Path:
        return self.root / relative

    def _write(self, relative: str, content: str) -> Path:
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _make_ready_files(self) -> None:
        config = {
            "llm_agent": {
                "enabled": False,
                "runtime": "pi",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
            },
            "pi_agent": {
                "enabled": True,
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "package": "@earendil-works/pi-coding-agent",
                "version": "0.84.2",
                "agent_home": "pi_runtime/agent_home",
                "compiled_skills_enabled": False,
            },
            "research_operations": {
                "roster_path": "config/research_roster.local.json",
                "restrictions_path": "config/research_restrictions.local.json",
            },
            "production_pilot": {
                "vendor_event_path": "data/production_pilot/vendor_events.jsonl",
            },
        }
        self._write("config/watchlist.local.json", json.dumps(config))
        self._write("config/research_roster.local.json", "{}\n")
        self._write("config/research_restrictions.local.json", "{}\n")
        self._write(
            "pyproject.toml",
            '[project]\nname = "stock-agent"\nversion = "0.1.0"\nrequires-python = ">=3.8"\n',
        )
        self._write("requirements.lock", "# locked test environment\n")
        self._write(
            "pi_runtime/package.json",
            json.dumps({
                "dependencies": {"@earendil-works/pi-coding-agent": "0.84.2"}
            }),
        )
        self._write("pi_runtime/package-lock.json", "{}\n")
        self._write(
            "pi_runtime/agent_home/settings.json",
            json.dumps({
                "defaultProjectTrust": "never",
                "defaultTools": [],
                "enableInstallTelemetry": False,
                "enableAnalytics": False,
                "compaction": {"enabled": False},
                "retry": {"enabled": False},
                "packages": [],
                "extensions": [],
                "skills": [],
                "prompts": [],
                "themes": [],
            }),
        )
        with project_path_override(lambda value: self.root / str(value)):
            source_fingerprint = build_release_candidate_identity()["fingerprint"]
        fingerprint = "b" * 64
        self._write(
            "reports/release/secret_scan_latest.json",
            json.dumps({
                "status": "PASS",
                "input_fingerprint": fingerprint,
                "release_candidate_fingerprint": source_fingerprint,
            }),
        )
        self._write(
            "reports/release/sbom.cdx.json",
            json.dumps({
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "components": [{"name": "stock-agent", "version": "0.1.0"}],
                "properties": [
                    {"name": "stock-agent:control-status", "value": "PASS"},
                    {"name": "stock-agent:input-fingerprint", "value": fingerprint},
                ],
            }),
        )
        self._write(
            "reports/release/license_report_latest.json",
            json.dumps({"status": "PASS", "input_fingerprint": fingerprint}),
        )
        self._write(
            "reports/release/python_environment_latest.json",
            json.dumps({
                "status": "PASS",
                "input_fingerprint": fingerprint,
                "release_candidate_fingerprint": source_fingerprint,
            }),
        )
        self._write(
            "reports/backup_restore/latest.json",
            json.dumps({
                "status": "PASS",
                "technical_status": "PASS",
                "production_readiness": "READY",
                "input_fingerprint": "d" * 64,
                "release_candidate_fingerprint": source_fingerprint,
            }),
        )

    @staticmethod
    def _git_runner(status: str = "", head: bool = True):
        def runner(command, cwd):
            args = tuple(command[1:])
            if args == ("rev-parse", "--show-toplevel"):
                return 0, str(cwd) + "\n", ""
            if args == ("rev-parse", "--verify", "HEAD"):
                if head:
                    return 0, "a" * 40 + "\n", ""
                return 128, "", "fatal: Needed a single revision"
            if args == ("branch", "--show-current"):
                return 0, "main\n", ""
            if args == ("status", "--porcelain=v1"):
                return 0, status, ""
            if args == ("show", "-s", "--format=%cI", "HEAD"):
                return 0, "2026-08-30T10:00:00+08:00\n", ""
            return 1, "", "unsupported"

        return runner

    def _build(self, **kwargs):
        with project_path_override(lambda value: self.root / str(value)):
            return build_release_audit(
                "config/watchlist.local.json",
                now=kwargs.pop("now", NOW),
                command_runner=kwargs.pop("command_runner", self._git_runner()),
                **kwargs,
            )

    def test_ready_manifest_requires_clean_git_and_control_artifacts(self):
        audit = self._build()
        self.assertEqual(audit["status"], "READY")
        self.assertTrue(audit["technical_release_ready"])
        self.assertEqual(audit["blockers"], [])
        self.assertEqual(len(audit["manifest_id"]), 64)
        self.assertEqual(audit["business_enablement"]["status"], "NOT_ASSERTED")
        self.assertFalse(audit["operation_contract"]["llm_call"])
        self.assertFalse(audit["operation_contract"]["business_state_write"])

    def test_missing_head_and_dirty_worktree_are_separate_blockers(self):
        audit = self._build(command_runner=self._git_runner("?? private-name.txt\n", head=False))
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertIn("Git HEAD is missing", "\n".join(audit["blockers"]))
        self.assertIn("worktree is not clean", "\n".join(audit["blockers"]))
        self.assertEqual(audit["git"]["change_counts"]["untracked"], 1)
        self.assertNotIn("private-name.txt", json.dumps(audit))

    def test_missing_python_lock_and_g0_reports_block_release(self):
        self._path("requirements.lock").unlink()
        self._path("reports/release/sbom.cdx.json").unlink()
        audit = self._build()
        blockers = "\n".join(audit["blockers"])
        self.assertIn("python_dependency_lock", blockers)
        self.assertIn("sbom", blockers)
        self.assertFalse(audit["technical_release_ready"])

    def test_invalid_or_review_required_control_artifact_blocks_release(self):
        self._write(
            "reports/release/license_report_latest.json",
            json.dumps({"status": "REVIEW_REQUIRED", "input_fingerprint": "c" * 64}),
        )
        audit = self._build()
        self.assertIn(
            "Required G0 control artifact is not PASS: license_report=REVIEW_REQUIRED",
            audit["blockers"],
        )

    def test_local_backup_rehearsal_cannot_clear_production_dr_gate(self):
        self._write(
            "reports/backup_restore/latest.json",
            json.dumps({
                "status": "PASS",
                "technical_status": "PASS",
                "production_readiness": "BLOCKED",
                "input_fingerprint": "d" * 64,
                "release_candidate_fingerprint": self._build()["release_candidate"]["fingerprint"],
            }),
        )
        audit = self._build()
        self.assertIn("Backup/restore production readiness is not READY", audit["blockers"])

    def test_source_change_after_controls_blocks_stale_evidence(self):
        self._write("stock_agent/new_logic.py", "VALUE = 1\n")
        audit = self._build()
        blockers = "\n".join(audit["blockers"])
        self.assertIn("stale for current source: secret_scan", blockers)
        self.assertIn("stale for current source: python_environment", blockers)

    def test_pi_package_mismatch_and_unsafe_settings_block_release(self):
        self._write(
            "pi_runtime/package.json",
            json.dumps({"dependencies": {"@earendil-works/pi-coding-agent": "9.9.9"}}),
        )
        self._write("pi_runtime/agent_home/settings.json", json.dumps({"defaultTools": ["shell"]}))
        audit = self._build()
        blockers = "\n".join(audit["blockers"])
        self.assertIn("isolated no-tool policy", blockers)
        self.assertIn("package version", blockers)

    def test_manifest_id_is_stable_across_generation_times(self):
        first = self._build(now=NOW)
        second = self._build(now=NOW + timedelta(days=1))
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["manifest_id"], second["manifest_id"])

    def test_secret_values_and_url_credentials_are_never_serialized(self):
        config_path = self._path("config/watchlist.local.json")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["llm_agent"].update({
            "enabled": True,
            "api_key_env": "RELEASE_AUDIT_TEST_KEY",
            "base_url": "https://user:pass@api.deepseek.com/v1?token=hidden#fragment",
        })
        config_path.write_text(json.dumps(config), encoding="utf-8")
        secret = "sk-" + "ReleaseAuditValueZ9Q8W7V6U5T4"
        with patch.dict(os.environ, {"RELEASE_AUDIT_TEST_KEY": secret}, clear=False):
            audit = self._build()
        serialized = json.dumps(audit, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("user:pass", serialized)
        self.assertNotIn("token=hidden", serialized)
        self.assertTrue(audit["models"]["llm_agent"]["credential_present"])
        self.assertFalse(audit["models"]["llm_agent"]["credential_value_recorded"])

    def test_stale_latest_projection_is_warning_not_technical_approval(self):
        health = self._write(
            "reports/health/latest.json",
            json.dumps({"generated_at": "2026-08-20T10:00:00+08:00", "overall_status": "OK"}),
        )
        stale_time = (NOW - timedelta(hours=100)).timestamp()
        os.utime(health, (stale_time, stale_time))
        audit = self._build()
        item = next(value for value in audit["latest_artifacts"] if value["label"] == "health")
        self.assertEqual(item["freshness"], "STALE")
        self.assertIn("Latest projection is stale: health", audit["warnings"])
        self.assertEqual(audit["business_enablement"]["status"], "NOT_ASSERTED")

    def test_adverse_source_status_is_visible_but_does_not_mutate_business_state(self):
        self._write(
            "reports/production_pilot/latest.json",
            json.dumps({
                "technical_readiness": "READY",
                "candidate_readiness": "BLOCKED",
                "go_live_decision": "NO_GO",
            }),
        )
        audit = self._build()
        pilot = next(value for value in audit["latest_artifacts"] if value["label"] == "production_pilot")
        self.assertEqual(pilot["reported_status"], "NO_GO")
        self.assertEqual(pilot["reported_statuses"]["technical_readiness"], "READY")
        self.assertIn("Latest projection reports an adverse state: production_pilot=NO_GO", audit["warnings"])
        self.assertEqual(audit["business_enablement"]["status"], "NOT_ASSERTED")

    def test_source_reported_go_never_becomes_automatic_business_go(self):
        self._write("reports/production_pilot/latest.json", json.dumps({"status": "GO"}))
        self._write("reports/business_onboarding/latest.json", json.dumps({"status": "READY"}))
        audit = self._build()
        business = audit["business_enablement"]
        self.assertEqual(business["production_pilot_reported_status"], "GO")
        self.assertEqual(business["status"], "NOT_ASSERTED")
        self.assertFalse(business["automatic_business_activation"])

    def test_write_creates_timestamped_and_latest_report_only(self):
        with project_path_override(lambda value: self.root / str(value)):
            result = write_release_audit(
                "config/watchlist.local.json",
                now=NOW,
                command_runner=self._git_runner(),
            )
        for key in ("release_audit_json_path", "release_audit_path", "latest_json_path", "latest_path"):
            self.assertTrue(Path(result[key]).is_file(), key)
        payload = json.loads(Path(result["latest_json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["manifest_id"], result["audit"]["manifest_id"])
        self.assertFalse(self._path("data/events/release.jsonl").exists())

    def test_markdown_states_release_and_business_boundaries(self):
        markdown = render_release_audit_markdown(self._build())
        self.assertIn("Technical release status: `READY`", markdown)
        self.assertIn("Business enablement: `NOT_ASSERTED`", markdown)
        self.assertIn("never grants business activation", markdown)
        self.assertIn("portfolio action: `false`", markdown)


if __name__ == "__main__":
    unittest.main()
