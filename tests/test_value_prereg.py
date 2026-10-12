"""GLM-12 preregistration review pack: see exactly what a study freeze locks."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.value_validation import (
    _policy_snapshot,
    build_preregistration_pack,
    object_hash,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))


class PreregPackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        base = load_config(str(Path(__file__).resolve().parents[1] / "config/watchlist.example.json"))
        self.config = dict(base)
        self.config["value_validation"] = {"enabled": True}

    def test_pack_is_read_only(self):
        pack = build_preregistration_pack(self.config)
        self.assertEqual(pack["study_id"], "a_share_research_value_v1")
        # No study side effects: the append-only event log is not even created.
        self.assertFalse((self.root / "data" / "value_validation" / "events.jsonl").exists())

    def test_criteria_carry_the_policy_fingerprint(self):
        pack = build_preregistration_pack(self.config)
        self.assertEqual(pack["criteria"]["fingerprint"],
                         object_hash(_policy_snapshot(load_config_watchlist(self.config))))
        self.assertEqual(pack["criteria"]["target_efficiency_improvement"], 0.20)

    def test_fingerprint_sensitive_to_target_change(self):
        changed = dict(self.config)
        changed["value_validation"] = {"enabled": True, "target_efficiency_improvement": 0.30}
        base_pack = build_preregistration_pack(self.config)
        changed_pack = build_preregistration_pack(changed)
        self.assertNotEqual(base_pack["criteria"]["fingerprint"],
                            changed_pack["criteria"]["fingerprint"])

    def test_targets_review_surfaces_the_plan_decision(self):
        pack = build_preregistration_pack(self.config)
        review = pack["targets_review"]
        self.assertEqual(review["plan_reviewable_target"], 0.30)
        self.assertEqual(review["currently_configured"], 0.20)
        self.assertIn("决定", review["note"])
        self.assertTrue(review["requires_user_decision"])

    def test_blocked_items_are_explicit(self):
        pack = build_preregistration_pack(self.config)
        blocked = " ".join(pack["blocked_items"])
        for marker in ("真实案件", "人工时间", "盲评", "揭盲"):
            self.assertIn(marker, blocked)
        self.assertTrue(pack["blocked_items"])

    def test_cli_prints_json_pack(self):
        import contextlib
        import io
        from unittest.mock import patch

        from stock_agent.cli import main

        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["stock_agent", "value-validation-prereg",
                                        "--config", str(config_file)]), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        payload = json.loads(captured.getvalue())
        self.assertIn("criteria", payload)
        self.assertIn("fingerprint", payload["criteria"])


def load_config_watchlist(config):
    """Rebuild the normalized cfg exactly as the module does (for hash parity)."""
    from stock_agent.value_validation import value_validation_config

    return value_validation_config(config)


if __name__ == "__main__":
    unittest.main()
