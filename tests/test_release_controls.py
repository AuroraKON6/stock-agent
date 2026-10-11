from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.release_controls import build_release_controls, write_release_controls


NOW = datetime(2026, 9, 1, 20, 30, 0, tzinfo=timezone(timedelta(hours=8)))


class ReleaseControlsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._prepare_project()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _prepare_project(self) -> None:
        config = {
            "pi_agent": {
                "skill_registry": "config/pi_skills.json",
                "skill_lock": "config/pi_skills.lock.json",
            }
        }
        self._write("config/watchlist.local.json", json.dumps(config))
        self._write("config/watchlist.example.json", "{}\n")
        self._write("pyproject.toml", '[project]\nname="stock-agent"\nversion="0.1.0"\n')
        self._write("requirements-optional.txt", "pyyaml\n")
        self._write(
            "requirements.lock.txt",
            "pyyaml==6.0.3 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        )
        self._write("stock_agent/app.py", "VALUE = 'safe'\n")
        self._write("tests/test_fixture.py", "TOKEN = 'sk-test-placeholder-1234567890'\n")
        self._write(
            "pi_runtime/package-lock.json",
            json.dumps({
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"safe-node": "1.2.3"}},
                    "node_modules/safe-node": {
                        "version": "1.2.3",
                        "license": "MIT",
                        "integrity": "sha512-" + "YQ==",
                    },
                },
            }),
        )
        self._write("pi_runtime/package.json", "{}\n")
        skill_text = "# Reviewed method\n"
        skill_path = self._write(".pi/skills/reviewed/SKILL.md", skill_text)
        import hashlib
        sha = hashlib.sha256(skill_path.read_bytes()).hexdigest()
        self._write(
            "config/pi_skills.json",
            json.dumps({
                "skills": [{
                    "id": "reviewed",
                    "version": "1.0.0",
                    "path": ".pi/skills/reviewed/SKILL.md",
                    "license": "Project-Owned",
                }]
            }),
        )
        self._write(
            "config/pi_skills.lock.json",
            json.dumps({"skills": [{"id": "reviewed", "sha256": sha}]}),
        )

    @staticmethod
    def _metadata():
        return {
            "pyyaml==6.0.3": {
                "name": "pyyaml",
                "version": "6.0.3",
                "license_expression": "MIT",
                "license": "",
                "license_classifiers": [],
            }
        }

    def _build(self, metadata=None):
        with project_path_override(lambda value: self.root / str(value)):
            return build_release_controls(
                "config/watchlist.local.json",
                python_metadata=self._metadata() if metadata is None else metadata,
                now=NOW,
            )

    def test_all_local_controls_pass_with_exact_metadata(self):
        result = self._build()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["statuses"], {
            "secret_scan": "PASS",
            "sbom": "PASS",
            "license_report": "PASS",
        })
        self.assertFalse(result["operation_contract"]["network_access"])
        self.assertFalse(result["operation_contract"]["business_state_write"])

    def test_placeholder_secret_is_counted_but_not_blocking(self):
        result = self._build()
        scan = result["secret_scan"]
        self.assertEqual(scan["status"], "PASS")
        self.assertEqual(scan["blocking_finding_count"], 0)
        self.assertEqual(scan["placeholder_finding_count"], 1)
        self.assertFalse(scan["findings"][0]["value_recorded"])

    def test_real_secret_is_blocking_and_never_serialized(self):
        secret = "sk-" + "LiveValueZ9Q8W7V6U5T4R3"
        self._write("stock_agent/credential.txt", secret + "\n")
        result = self._build()
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["secret_scan"]["status"], "FAIL")
        self.assertEqual(result["secret_scan"]["blocking_finding_count"], 1)
        self.assertNotIn(secret, serialized)
        self.assertIn("match_sha256", serialized)

    def test_local_ignored_config_is_still_secret_scanned(self):
        secret = "sk-" + "LocalValueZ9Q8W7V6U5T4R3"
        config = json.loads((self.root / "config/watchlist.local.json").read_text(encoding="utf-8"))
        config["unsafe_literal"] = secret
        self._write("config/watchlist.local.json", json.dumps(config))
        result = self._build()
        finding = next(item for item in result["secret_scan"]["findings"] if item["classification"] == "BLOCKING")
        self.assertEqual(finding["scope"], "local_sensitive")
        self.assertNotIn(secret, json.dumps(result))

    def test_sbom_contains_python_npm_and_pi_skill_components(self):
        result = self._build()
        components = result["sbom"]["components"]
        ecosystems = {
            prop["value"]
            for component in components
            for prop in component.get("properties", [])
            if prop.get("name") == "stock-agent:ecosystem"
        }
        self.assertEqual(ecosystems, {"pypi", "npm", "pi-skill"})
        self.assertTrue(str(result["sbom"]["serialNumber"]).startswith("urn:uuid:"))

    def test_unknown_exact_version_license_requires_review(self):
        with patch("stock_agent.release_controls.importlib_metadata.distribution", side_effect=LookupError):
            result = self._build(metadata={})
        self.assertEqual(result["license_report"]["status"], "REVIEW_REQUIRED")
        self.assertGreater(result["license_report"]["review_required_count"], 0)
        self.assertFalse(result["license_report"]["policy"]["automatic_legal_approval"])

    def test_write_persists_only_release_reports(self):
        with project_path_override(lambda value: self.root / str(value)):
            output = write_release_controls(
                "config/watchlist.local.json",
                python_metadata=self._metadata(),
                now=NOW,
            )
        for key in (
            "secret_scan_path", "sbom_path", "license_report_path",
            "metadata_cache_path", "summary_json_path", "summary_path",
        ):
            self.assertTrue(Path(output[key]).is_file(), key)
        self.assertFalse((self.root / "data/events/release_controls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
