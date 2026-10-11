"""Manifest consumption tests: scheduled summary binds to the exact manifest.

Covers the acceptance-review leftover "run_manifest 在 schedule/pilot 的完整
消费" — a scheduled-trigger coordinated pipeline produces an eligible manifest,
the scheduler summary binds to it, and any tampering is BLOCKED. Offline.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.models import RawSignal, now_iso
from stock_agent.run_manifest import bind_scheduled_summary


class ManifestBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {
            "storage": {"sqlite_path": str(self.root / "data" / "signals.sqlite3")},
            "sources": {},
            "topics": [{"name": "runtime", "symbols": [], "keywords": ["major contract"], "risk_keywords": []}],
            "priority_rules": {
                "p0_keywords": [], "p1_keywords": ["major contract"],
                "positive_keywords": [], "negative_keywords": [], "review_keywords": [],
            },
            "llm_agent": {"enabled": False},
            "multi_role_agent": {"enabled": False},
            "research_cases": {"enabled": False, "llm_enabled": False},
            "research_framework": {"enabled": False},
            "research_team": {"enabled": False},
            "research_runtime": {"enabled": False, "auto_sync": False},
            "research_workpapers": {"enabled": False},
            "report": {"include_low_priority": True},
            "run_coordination": {
                "enabled": True,
                "lease_root": str(self.root / "data" / "leases"),
                "recovered_lease_root": str(self.root / "data" / "recovered"),
                "checkpoint_path": str(self.root / "data" / "checkpoints.jsonl"),
                "report_dir": str(self.root / "reports" / "coordination"),
                "lease_ttl_seconds": 30,
                "require_explicit_stale_recovery": True,
                "fail_fast_on_lease_conflict": True,
            },
            "run_manifest": {
                "enabled": True,
                "manifest_dir": str(self.root / "data" / "manifests"),
                "pointer_path": str(self.root / "reports" / "latest_manifest.json"),
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _run_scheduled_pipeline(self):
        from stock_agent.pipeline import run_agent_pipeline

        config = dict(self.config)
        config["_trigger_source"] = "scheduled"
        raw = RawSignal(
            source="manual", source_type="news", title="A major contract announcement",
            summary="major contract", url="https://example.test/news", published_at=now_iso(), payload={},
        )
        with project_path_override(lambda value, r=self.root: r / value), \
             patch("stock_agent.pipeline.collect_sources", return_value=[raw]):
            result = run_agent_pipeline(config)
        pointer = json.loads((self.root / "reports" / "latest_manifest.json").read_text(encoding="utf-8"))
        return result, pointer

    def _write_summary(self, pointer, *, sha_override=None, mode="daily"):
        summary = {
            "mode": mode,
            "exit_code": 0,
            "trigger_source": "scheduled",
            "run_id": pointer["run_id"],
            "run_manifest_path": pointer["manifest_path"],
            "run_manifest_sha256": sha_override or pointer["manifest_sha256"],
            "report_path": str(self.root / pointer["manifest_path"]).replace("\\", "/"),
        }
        # bind expects report_path resolvable under the workspace; the manifest
        # json_report already pins it — reuse the manifest's own reference.
        manifest_abs = self.root / pointer["manifest_path"]
        manifest = json.loads(manifest_abs.read_text(encoding="utf-8"))
        json_ref = [item for item in manifest["artifacts"] if item["name"] == "json_report"][0]
        summary["report_path"] = json_ref["path"]
        path = self.root / "logs" / "scheduled" / "run_test.summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        return path

    def test_scheduled_run_binds_pass(self):
        result, pointer = self._run_scheduled_pipeline()
        self.assertEqual(result.stats["run_manifest"]["status"], "RECORDED")
        self.assertTrue(result.stats["run_manifest"]["eligible_for_pilot"],
                        "scheduled agent-run manifest must be pilot-eligible")
        summary_path = self._write_summary(pointer)
        with project_path_override(lambda value, r=self.root: r / value):
            bound = bind_scheduled_summary(self.config, str(summary_path))
        self.assertEqual(bound["status"], "PASS", bound.get("errors"))
        self.assertEqual(bound["manifest"]["run_id"], pointer["run_id"])

    def test_tampered_manifest_hash_blocks_binding(self):
        _, pointer = self._run_scheduled_pipeline()
        summary_path = self._write_summary(pointer, sha_override="0" * 64)
        with project_path_override(lambda value, r=self.root: r / value):
            bound = bind_scheduled_summary(self.config, str(summary_path))
        self.assertEqual(bound["status"], "BLOCKED")
        self.assertTrue(any("hash" in str(error).lower() for error in bound["errors"]))

    def test_manual_trigger_manifest_is_not_pilot_eligible(self):
        from stock_agent.pipeline import run_agent_pipeline

        config = dict(self.config)  # no _trigger_source -> manual
        raw = RawSignal(
            source="manual", source_type="news", title="manual run",
            summary="s", url="https://example.test/m", published_at=now_iso(), payload={},
        )
        with project_path_override(lambda value, r=self.root: r / value), \
             patch("stock_agent.pipeline.collect_sources", return_value=[raw]):
            result = run_agent_pipeline(config)
        self.assertFalse(result.stats["run_manifest"]["eligible_for_pilot"])


if __name__ == "__main__":
    unittest.main()
