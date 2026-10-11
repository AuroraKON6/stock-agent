from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.research_operations import (
    ResearchOperationsPolicyError,
    add_business_hours,
    authorize_runtime_action,
    build_research_operations_snapshot,
    load_operations_inputs,
    validate_runtime_action,
)
from stock_agent.research_runtime import _evaluate_gates, _is_automated_actor, record_runtime_action


def _principal(actor_id, roles, principal_type="human", *, coverage=None, maximum=10):
    authorizations = []
    if "compliance_officer" in roles:
        authorizations.append("compliance_authorized")
    if {"publication_approver", "research_supervisor"}.intersection(roles):
        authorizations.append("publication_authorized")
    return {
        "actor_id": actor_id,
        "display_name": actor_id,
        "principal_type": principal_type,
        "active": True,
        "roles": list(roles),
        "information_side": "public",
        "qualifications": {
            "securities_analyst_registration": {
                "status": "verified",
                "registration_id": "REG-{0}".format(actor_id),
            }
        },
        "authorizations": authorizations,
        "coverage_entities": coverage or ["*"],
        "restricted_entities": [],
        "max_active_cases": maximum,
    }


def _roster():
    return [
        _principal("human:coordinator", ["research_coordinator"]),
        _principal("human:lead", ["lead_analyst"]),
        _principal("human:quality", ["quality_reviewer"]),
        _principal("human:compliance", ["compliance_officer"]),
        _principal("human:publisher", ["publication_approver", "research_supervisor"]),
        _principal("service:source", ["information_analyst"], "service", maximum=20),
        _principal("service:risk", ["risk_reviewer"], "service", maximum=20),
    ]


def _config():
    return {
        "research_runtime": {
            "require_primary_source_for": ["P0", "P1"],
            "required_compliance_checks": [],
        },
        "research_operations": {
            "enabled": True,
            "enforce_roster": True,
            "assignment_mode": "suggest_only",
            "roster_path": "config/roster.json",
            "restrictions_path": "config/restrictions.json",
            "require_recorded_challenge": True,
            "timezone": "Asia/Shanghai",
            "business_calendar": {
                "weekdays": [0, 1, 2, 3, 4],
                "workday_start": "08:30",
                "workday_end": "18:00",
                "holidays": [],
            },
        },
    }


def _case():
    return {
        "case_id": "case001",
        "title": "Case 001",
        "entity": "600000",
        "topic": "earnings",
        "top_priority": "P1",
        "stage": "evidence_collection",
        "stage_entered_at": "2026-08-10T08:30:00+08:00",
        "created_at": "2026-08-10T08:30:00+08:00",
        "updated_at": "2026-08-10T08:30:00+08:00",
        "assignments": {},
        "task_assignments": {},
        "task_overrides": {},
        "manual_workpapers": [],
        "manual_evidence_requests": [],
        "action_history": [],
        "source_types": ["filing"],
        "source_case": {},
        "evidence_facts": {
            "traceable_source_count": 0,
            "primary_source_count": 0,
            "source_error_count": 0,
            "evidence_gaps": ["missing filing"],
        },
        "approvals": {
            "quality_review": {"status": "pending"},
            "compliance_review": {"status": "pending"},
            "publication": {"status": "pending"},
        },
        "workstreams": [
            {
                "task_id": "source_verification",
                "owner_role": "information_analyst",
                "status": "ready",
                "assignee": "",
            }
        ],
    }


class ResearchOperationsTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "config").mkdir(parents=True)
        self.config = _config()
        self._write_inputs(_roster(), [])
        self.paths = project_path_override(lambda value: self.root / value)
        self.paths.__enter__()

    def tearDown(self):
        self.paths.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _write_inputs(self, principals, restrictions):
        (self.root / "config" / "roster.json").write_text(
            json.dumps({"principals": principals}, ensure_ascii=False), encoding="utf-8"
        )
        (self.root / "config" / "restrictions.json").write_text(
            json.dumps({"entries": restrictions}, ensure_ascii=False), encoding="utf-8"
        )

    def _write_runtime(self, case=None):
        path = self.root / "data" / "research_runtime" / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"runtime_version": "1.0", "cases": [case or _case()]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_roster_has_all_mandatory_human_capabilities(self):
        inputs = load_operations_inputs(self.config)
        self.assertTrue(inputs["roster_valid"])
        self.assertEqual([], inputs["missing_mandatory_human_capabilities"])

    def test_missing_human_capabilities_are_reported_without_invention(self):
        self._write_inputs([_principal("service:source", ["information_analyst"], "service")], [])
        inputs = load_operations_inputs(self.config)
        self.assertIn("lead_analyst", inputs["missing_mandatory_human_capabilities"])
        self.assertIn("compliance_officer", inputs["missing_mandatory_human_capabilities"])

    def test_unregistered_actor_is_denied_with_auditable_decision(self):
        decision = authorize_runtime_action(
            self.config, _case(), "assign_lead", "human:unknown", "research_coordinator", assignee="human:lead"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("not registered", decision["reasons"][0])
        with self.assertRaises(ResearchOperationsPolicyError):
            validate_runtime_action(
                self.config, _case(), "assign_lead", "human:unknown", "research_coordinator", assignee="human:lead"
            )

    def test_human_coordinator_can_assign_eligible_lead(self):
        decision = validate_runtime_action(
            self.config,
            _case(),
            "assign_lead",
            "human:coordinator",
            "research_coordinator",
            assignee="human:lead",
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual("human", decision["principal_type"])

    def test_service_principal_cannot_pass_human_gate(self):
        case = _case()
        case["assignments"]["quality_reviewer"] = "service:risk"
        decision = authorize_runtime_action(
            self.config, case, "quality_approve", "service:risk", "quality_reviewer"
        )
        self.assertFalse(decision["allowed"])

    def test_active_restriction_requires_authorization_metadata(self):
        self._write_inputs(_roster(), [{"entity": "600000", "list_type": "restricted"}])
        inputs = load_operations_inputs(self.config)
        self.assertFalse(inputs["roster_valid"])
        self.assertIn("requires reason and authorized_by", inputs["errors"][0])

    def test_restricted_entity_is_l3_and_has_no_public_research_candidate(self):
        self._write_inputs(
            _roster(),
            [{
                "entity": "600000",
                "list_type": "restricted",
                "status": "active",
                "reason": "conflict",
                "authorized_by": "human:compliance",
            }],
        )
        snapshot = build_research_operations_snapshot(
            self.config, {"cases": [_case()]}, now=datetime(2026, 8, 11, 10, tzinfo=timezone(timedelta(hours=8))), write=False
        )
        item = snapshot["cases"][0]
        self.assertEqual("restricted", item["access_classification"])
        self.assertEqual("L3", item["escalation_level"])
        self.assertEqual([], item["eligible_assignees"])

    def test_capacity_and_coverage_filter_candidates(self):
        roster = _roster()
        source = next(item for item in roster if item["actor_id"] == "service:source")
        source["coverage_entities"] = ["000001"]
        self._write_inputs(roster, [])
        snapshot = build_research_operations_snapshot(self.config, {"cases": [_case()]}, write=False)
        self.assertEqual([], snapshot["cases"][0]["eligible_assignees_by_role"]["information_analyst"])

    def test_business_hours_skip_weekend(self):
        cfg = load_operations_inputs(self.config)["config"]
        china_tz = timezone(timedelta(hours=8))
        start = datetime(2026, 8, 7, 17, 0, tzinfo=china_tz)
        due = add_business_hours(start, 3, cfg)
        self.assertEqual(datetime(2026, 8, 10, 10, 30, tzinfo=china_tz), due)

    def test_snapshot_is_recommendation_only_and_does_not_mutate_runtime(self):
        runtime = {"cases": [_case()]}
        before = copy.deepcopy(runtime)
        snapshot = build_research_operations_snapshot(self.config, runtime, write=False)
        self.assertEqual(before, runtime)
        self.assertTrue(snapshot["cases"][0]["recommendation_only"])
        self.assertFalse(snapshot["cases"][0]["automatic_assignment_executed"])

    def test_snapshot_writes_latest_and_immutable_history(self):
        snapshot = build_research_operations_snapshot(self.config, {"cases": [_case()]}, write=True)
        for key in [
            "research_operations_data_path",
            "research_operations_report_path",
            "research_operations_history_json_path",
            "research_operations_history_report_path",
        ]:
            self.assertTrue(Path(snapshot[key]).exists(), key)

    def test_assign_workstream_is_persisted_with_authorization_audit(self):
        self._write_runtime()
        result = record_runtime_action(
            "case001",
            "assign_workstream",
            "human:coordinator",
            "research_coordinator",
            task_id="source_verification",
            assignee="service:source",
            config=self.config,
        )
        self.assertEqual("service:source", result["case"]["task_assignments"]["source_verification"])
        self.assertTrue(result["record"]["authorization"]["allowed"])
        self.assertEqual("20.0", result["record"]["operations_policy_version"])

    def test_only_assigned_workstream_principal_can_complete_task(self):
        case = _case()
        case["task_assignments"]["source_verification"] = "service:source"
        with self.assertRaises(ResearchOperationsPolicyError):
            validate_runtime_action(
                self.config,
                case,
                "complete_task",
                "human:coordinator",
                "information_analyst",
                task_id="source_verification",
            )

    def test_llm_risk_text_does_not_complete_recorded_challenge(self):
        case = _case()
        case["top_priority"] = "P2"
        case["source_types"] = ["news"]
        case["evidence_facts"] = {
            "traceable_source_count": 1,
            "primary_source_count": 0,
            "source_error_count": 0,
            "evidence_gaps": [],
            "has_hypothesis": True,
            "has_risk_challenge": True,
            "has_fundamental_analysis": True,
            "has_market_context": True,
        }
        cfg = {
            "require_primary_source_for": ["P0", "P1"],
            "require_recorded_challenge": True,
        }
        gates = _evaluate_gates(case, cfg)
        self.assertEqual("PASS", gates["analysis"]["status"])
        self.assertEqual("BLOCKED", gates["challenge"]["status"])
        case["assignments"]["lead_analyst"] = "human:lead"
        case["task_overrides"]["independent_challenge"] = {
            "status": "completed",
            "actor": "service:risk",
            "actor_role": "risk_reviewer",
            "references": ["risk_note:data/workpapers/case001-risk.md"],
        }
        self.assertEqual("PASS", _evaluate_gates(case, cfg)["challenge"]["status"])

    def test_legacy_configuration_remains_backward_compatible(self):
        decision = authorize_runtime_action(
            {}, _case(), "quality_approve", "legacy-human", "quality_reviewer"
        )
        self.assertTrue(decision["allowed"])
        self.assertFalse(decision["enforced"])

    def test_stock_agent_identity_is_automated_even_without_roster_enforcement(self):
        self.assertTrue(_is_automated_actor("stock_agent_risk_guard"))


if __name__ == "__main__":
    unittest.main()
