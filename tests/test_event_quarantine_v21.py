import unittest
from unittest.mock import patch

from stock_agent.brokerage_architecture import project_brokerage_state


def _event(event_type, entity_type, entity_id, payload=None, source="stock_agent", actor="system"):
    return {
        "event_id": "{0}:{1}:{2}".format(event_type, entity_type, entity_id),
        "occurred_at": "2026-08-11T09:00:00+08:00",
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source": source,
        "actor": actor,
        "payload": payload or {},
    }


class EventProjectionQuarantineTests(unittest.TestCase):
    @patch("stock_agent.brokerage_architecture.load_events", return_value=[])
    @patch("stock_agent.brokerage_architecture.load_compact_events", return_value=[])
    def test_valid_test_quarantine_hides_projection_but_retains_raw_audit_count(self, _compact, _raw):
        events = [
            _event("signal_analyzed", "signal", "prod", {"source": "eastmoney", "priority": "P2"}),
            _event("signal_analyzed", "signal", "uat", {"source": "llm_governance_uat", "priority": "P1"}),
            _event(
                "test_artifact_quarantined",
                "signal",
                "uat",
                {"scope": "test_only", "test_source": "llm_governance_uat", "reason": "isolated UAT"},
                source="system_maintenance",
                actor="system_maintenance",
            ),
        ]
        state = project_brokerage_state(events)
        self.assertEqual(set(state["objects"]["signals"]), {"prod"})
        self.assertEqual(state["projection_quarantine"]["entity_count"], 1)
        self.assertEqual(state["event_health"]["total_events"], 3)

    @patch("stock_agent.brokerage_architecture.load_events", return_value=[])
    @patch("stock_agent.brokerage_architecture.load_compact_events", return_value=[])
    def test_untrusted_quarantine_event_cannot_hide_production_entity(self, _compact, _raw):
        events = [
            _event("signal_analyzed", "signal", "prod", {"source": "eastmoney", "priority": "P2"}),
            _event(
                "test_artifact_quarantined",
                "signal",
                "prod",
                {"scope": "test_only", "test_source": "llm_governance_uat", "reason": "malicious"},
                source="stock_agent",
                actor="system",
            ),
        ]
        state = project_brokerage_state(events)
        self.assertIn("prod", state["objects"]["signals"])
        self.assertEqual(state["projection_quarantine"]["entity_count"], 0)


if __name__ == "__main__":
    unittest.main()
