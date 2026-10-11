"""GLM-02 source registry contract: structure, fail-closed lookups, no auto-approval."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.sources.registry import (
    APPROVAL_STATES,
    load_source_registry,
    may_send_to_llm,
    registry_status,
    validate_registry_entries,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))


def entry(**overrides):
    base = {
        "source_id": "manual-intel-pool",
        "kind": "manual_intel",
        "upstream": "operator-manual-input",
        "purpose": "operator-curated intelligence inbox for personal research",
        "approval": {"state": "pending_human_approval", "approved_by": "", "approved_at": ""},
        "body_retention_allowed": True,
        "llm_external_send_allowed": False,
        "timeliness": {"expected_max_delay_minutes": 1440, "staleness_policy": "flag"},
        "rate_limit": {"max_requests_per_minute": 1, "max_requests_per_day": 100},
        "expires_at": "2099-12-31",
    }
    base.update(overrides)
    return base


class SourceRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))

    def write_registry(self, entries):
        path = self.root / "registry.json"
        path.write_text(json.dumps({"schema_version": "1.0", "entries": entries},
                                   ensure_ascii=False), encoding="utf-8")
        return {"source_registry": {"path": "registry.json"}}

    def test_valid_registry_loads_and_lookup_works(self):
        config = self.write_registry([entry()])
        registry = load_source_registry(config)
        status = registry_status(registry, "manual-intel-pool")
        self.assertTrue(status["registered"])
        self.assertFalse(status["expired"])
        self.assertEqual(status["approval_state"], "pending_human_approval")
        self.assertFalse(may_send_to_llm(registry, "manual-intel-pool"))

    def test_unregistered_source_fails_closed(self):
        registry = load_source_registry(self.write_registry([entry()]))
        status = registry_status(registry, "totally-unknown-source")
        self.assertFalse(status["registered"])
        self.assertFalse(may_send_to_llm(registry, "totally-unknown-source"))
        self.assertEqual(status["reason"], "not_registered")

    def test_expired_entry_is_not_usable(self):
        config = self.write_registry([entry(expires_at="2020-01-01")])
        registry = load_source_registry(config)
        status = registry_status(registry, "manual-intel-pool")
        self.assertTrue(status["registered"])
        self.assertTrue(status["expired"])
        self.assertFalse(may_send_to_llm(registry, "manual-intel-pool"))

    def test_external_send_requires_real_approval(self):
        config = self.write_registry([entry(
            llm_external_send_allowed=True,
            approval={"state": "pending_human_approval", "approved_by": "", "approved_at": ""},
        )])
        with self.assertRaises(ValueError) as raised:
            load_source_registry(config)
        self.assertIn("llm_external_send_allowed", str(raised.exception))

    def test_approval_by_service_account_is_rejected(self):
        config = self.write_registry([entry(
            llm_external_send_allowed=True,
            approval={"state": "approved", "approved_by": "stock_agent_service", "approved_at": "2026-10-01T00:00:00+08:00"},
        )])
        with self.assertRaises(ValueError) as raised:
            load_source_registry(config)
        self.assertIn("human", str(raised.exception).lower())

    def test_pseudo_multi_source_rejected(self):
        second = entry(source_id="other-name-same-upstream", kind="manual_intel",
                       upstream="operator-manual-input")
        config = self.write_registry([entry(), second])
        with self.assertRaises(ValueError) as raised:
            load_source_registry(config)
        self.assertIn("upstream", str(raised.exception).lower())

    def test_strict_schema_rejects_unknown_fields(self):
        config = self.write_registry([entry(unexpected_field="nope")])
        with self.assertRaises(ValueError) as raised:
            load_source_registry(config)
        self.assertIn("unexpected", str(raised.exception).lower())

    def test_missing_required_field_rejected(self):
        broken = entry()
        del broken["rate_limit"]
        with self.assertRaises(ValueError):
            load_source_registry(self.write_registry([broken]))

    def test_bad_approval_state_rejected(self):
        broken = entry(approval={"state": "auto", "approved_by": "", "approved_at": ""})
        with self.assertRaises(ValueError):
            validate_registry_entries([broken])
        self.assertIn("pending_human_approval", APPROVAL_STATES)

    def test_configured_but_missing_file_fails_closed(self):
        with self.assertRaises(ValueError):
            load_source_registry({"source_registry": {"path": "missing-registry.json"}})

    def test_no_registry_config_means_nothing_approved(self):
        registry = load_source_registry({})
        self.assertEqual(registry["entries"], {})
        self.assertFalse(may_send_to_llm(registry, "any-source"))

    def test_shipped_example_registry_is_valid(self):
        raw = (Path(__file__).resolve().parents[1]
               / "config" / "source_registry.example.json").read_text(encoding="utf-8")
        document = json.loads(raw)
        entries = validate_registry_entries(document["entries"])
        for item in entries:
            self.assertFalse(item["llm_external_send_allowed"],
                             "example registry must not ship with external send approved")
            self.assertEqual(item["approval"]["state"], "pending_human_approval")


if __name__ == "__main__":
    unittest.main()
