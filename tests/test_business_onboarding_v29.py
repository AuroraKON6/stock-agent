from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.business_onboarding import (
    PHASE_ORDER,
    build_business_onboarding,
    build_business_onboarding_preflight,
    generate_business_onboarding_pack,
)
from stock_agent.config import project_path_override
from stock_agent.control_evidence import (
    DEFAULT_CATEGORY_REVIEW_ROLES,
    ControlEvidencePolicyError,
    load_control_evidence_registry,
    register_control_evidence,
    resolve_verified_control_evidence,
    review_control_evidence,
    revoke_control_evidence,
    validate_managed_archive_reference,
)
from stock_agent.dashboard import _section_business_onboarding
from stock_agent.doctor import _check_business_onboarding
from stock_agent.production_pilot import (
    ProductionPilotPolicyError,
    _critical_input_hashes,
    _require_onboarding_ready_approver,
    _scheduled_sequence_status,
)
from stock_agent.provider_registry import (
    DEFAULT_EVIDENCE_FIELDS,
    PROVIDER_EVIDENCE_CATEGORIES,
    activate_provider,
    initialize_provider_candidate,
    load_active_provider_ids,
    record_provider_review,
    submit_provider_due_diligence,
)


class BusinessOnboardingV29TestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.override = project_path_override(lambda value: self.root / str(value))
        self.override.__enter__()
        self.source_counter = 0
        self.config = self._config()
        self.principals = self._principals()
        self._write_roster()
        self._write_json("config/restrictions.json", {"entries": []})

    def tearDown(self):
        self.override.__exit__(None, None, None)
        self.temp.cleanup()

    def _config(self):
        return {
            "business_onboarding": {
                "enabled": True,
                "enforce": True,
                "organization_id": "",
                "control_evidence_event_path": "data/business_onboarding/control_evidence_events.jsonl",
                "report_dir": "reports/business_onboarding",
                "intake_pack_dir": "reports/business_onboarding/intake_pack",
                "require_verified_control_evidence": True,
                "require_distinct_humans": True,
            },
            "research_operations": {
                "enabled": True,
                "enforce_roster": True,
                "roster_path": "config/roster.json",
                "restrictions_path": "config/restrictions.json",
            },
            "production_pilot": {
                "enabled": True,
                "enforce": True,
                "mode": "shadow",
                "minimum_active_providers": 2,
                "required_provider_scopes": ["internal_research", "external_llm"],
                "required_provider_review_roles": ["compliance_officer", "information_security_officer"],
                "required_provider_evidence": list(DEFAULT_EVIDENCE_FIELDS),
                "vendor_event_path": "data/production_pilot/vendor_events.jsonl",
                "required_approval_gates": {
                    "research_readiness": "lead_analyst",
                    "quality_readiness": "quality_reviewer",
                    "compliance_readiness": "compliance_officer",
                    "information_security_readiness": "information_security_officer",
                    "data_readiness": "data_steward",
                    "publication_readiness": "publication_approver",
                },
                "provider_activation_roles": ["data_steward", "research_coordinator"],
            },
        }

    def _principals(self):
        return [
            self._human("human:coord", ["research_coordinator"], ["research_coordination_authorized"]),
            self._human("human:lead", ["lead_analyst"], analyst=True),
            self._human("human:quality", ["quality_reviewer"], analyst=True),
            self._human("human:compliance", ["compliance_officer"], ["compliance_authorized"]),
            self._human(
                "human:infosec", ["information_security_officer"], ["information_security_authorized"]
            ),
            self._human("human:data", ["data_steward"], ["vendor_data_steward_authorized"]),
            self._human("human:publisher", ["publication_approver"], ["publication_authorized"]),
            self._human("human:records", ["information_analyst"]),
            self._human(
                "human:coord-review", ["research_coordinator"], ["research_coordination_authorized"]
            ),
            self._human(
                "human:compliance-review", ["compliance_officer"], ["compliance_authorized"]
            ),
            self._human(
                "human:infosec-review",
                ["information_security_officer"],
                ["information_security_authorized"],
            ),
            self._human("human:data-review", ["data_steward"], ["vendor_data_steward_authorized"]),
            self._human(
                "human:dual-review",
                ["compliance_officer", "information_security_officer"],
                ["compliance_authorized", "information_security_authorized"],
            ),
        ]

    @staticmethod
    def _human(actor_id, roles, authorizations=None, analyst=False):
        qualifications = {}
        if analyst:
            qualifications = {
                "securities_analyst_registration": {
                    "status": "verified",
                    "registration_id": "TEST-{0}".format(actor_id.split(":")[-1]),
                    "verified_at": "2026-01-01T00:00:00+08:00",
                    "evidence_ref": "",
                }
            }
        return {
            "actor_id": actor_id,
            "display_name": actor_id,
            "principal_type": "human",
            "active": True,
            "roles": list(roles),
            "information_side": "public",
            "qualifications": qualifications,
            "authorizations": list(authorizations or []),
            "onboarding_evidence_refs": {},
            "coverage_entities": ["*"],
            "restricted_entities": [],
            "max_active_cases": 10,
        }

    def _write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _write_roster(self):
        self._write_json("config/roster.json", {"principals": self.principals})

    def _source_file(self, label="evidence"):
        self.source_counter += 1
        path = self.root / "operator_intake" / "{0}_{1}.txt".format(label, self.source_counter)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("confidential test control bytes {0}".format(self.source_counter), encoding="utf-8")
        return path

    def _register(self, category, subject_id, *, actor="human:records", actor_role="information_analyst", **kwargs):
        valid_to = kwargs.pop("valid_to", "")
        if category in {
            "human_qualification", "human_training", "provider_data_rights",
            "provider_security", "provider_operations",
        } and not valid_to:
            valid_to = "2099-12-31T23:59:59+08:00"
        result = register_control_evidence(
            self.config,
            file_path=str(self._source_file(category)),
            category=category,
            title="{0} evidence".format(category),
            subject_id=subject_id,
            owner_role=actor_role,
            external_archive_ref="records://internal/{0}/{1}".format(category, self.source_counter),
            actor=actor,
            actor_role=actor_role,
            valid_from=kwargs.pop("valid_from", ""),
            valid_to=valid_to,
            notes=kwargs.pop("notes", "test-only evidence metadata"),
            **kwargs,
        )
        return result["record"]["evidence_id"]

    def _register_verified(self, category, subject_id):
        evidence_id = self._register(category, subject_id)
        actor_by_role = {
            "research_coordinator": "human:coord-review",
            "compliance_officer": "human:compliance-review",
            "information_security_officer": "human:infosec-review",
            "data_steward": "human:data-review",
        }
        for role in DEFAULT_CATEGORY_REVIEW_ROLES[category]:
            review_control_evidence(
                self.config,
                evidence_id=evidence_id,
                decision="approve",
                actor=actor_by_role[role],
                actor_role=role,
                notes="independent review completed for test fixture",
                review_record_ref="records://internal/reviews/{0}/{1}".format(
                    evidence_id.replace(":", "_"), role
                ),
            )
        return evidence_id

    def _onboard_organization(self):
        organization_id = "organization:test-broker-research"
        self.config["business_onboarding"]["organization_id"] = organization_id
        for category in (
            "governance_mandate", "internal_research_scope", "prohibited_actions_policy",
            "production_archive_contract", "production_archive_security", "production_archive_retention",
        ):
            self._register_verified(category, organization_id)
        return organization_id

    def _onboard_required_humans(self):
        required = {
            "human:coord": "research_coordinator",
            "human:lead": "lead_analyst",
            "human:quality": "quality_reviewer",
            "human:compliance": "compliance_officer",
            "human:infosec": "information_security_officer",
            "human:data": "data_steward",
            "human:publisher": "publication_approver",
        }
        principal_map = {item["actor_id"]: item for item in self.principals}
        for actor, role in required.items():
            categories = ["human_employment", "human_confidentiality", "human_training", "role_appointment"]
            if role in {"lead_analyst", "quality_reviewer"}:
                categories.append("human_qualification")
            refs = {}
            for category in categories:
                refs[category] = self._register_verified(category, actor)
            principal_map[actor]["onboarding_evidence_refs"] = refs
            if "human_qualification" in refs:
                principal_map[actor]["qualifications"]["securities_analyst_registration"]["evidence_ref"] = refs[
                    "human_qualification"
                ]
        self._write_roster()

    def _onboard_provider(self, provider_id):
        control_ids = {
            category: self._register_verified(category, provider_id)
            for category in (
                "provider_legal_contract", "provider_data_rights", "provider_security", "provider_operations"
            )
        }
        initialize_provider_candidate(
            self.config,
            provider_id,
            "{0} Legal Entity".format(provider_id),
            ["internal_research", "external_llm"],
        )
        evidence = {
            field: control_ids[PROVIDER_EVIDENCE_CATEGORIES[field][0]]
            for field in DEFAULT_EVIDENCE_FIELDS
        }
        submit_provider_due_diligence(
            self.config,
            provider_id,
            "human:data",
            "data_steward",
            evidence,
            notes="manual due diligence submitted after document verification",
        )
        record_provider_review(
            self.config,
            provider_id,
            "approve",
            "human:compliance",
            "compliance_officer",
            notes="legal and data-rights review completed",
            evidence_refs=[control_ids["provider_legal_contract"]],
        )
        record_provider_review(
            self.config,
            provider_id,
            "approve",
            "human:infosec",
            "information_security_officer",
            notes="security and continuity review completed",
            evidence_refs=[control_ids["provider_security"]],
        )
        activate_provider(
            self.config,
            provider_id,
            "human:data",
            "data_steward",
            notes="manual activation after independent approvals",
        )
        return control_ids


class ControlEvidenceRegistryTests(BusinessOnboardingV29TestCase):
    def test_registration_requires_rostered_human_and_stores_no_raw_file_or_path(self):
        source = self._source_file("mandate")
        with self.assertRaisesRegex(ControlEvidencePolicyError, "registered"):
            register_control_evidence(
                self.config,
                file_path=str(source),
                category="governance_mandate",
                title="Mandate",
                subject_id="organization:test",
                owner_role="system",
                external_archive_ref="records://internal/mandate/1",
                actor="system:stock_agent",
                actor_role="system",
            )
        evidence_id = self._register("governance_mandate", "organization:test")
        ledger = (self.root / "data/business_onboarding/control_evidence_events.jsonl").read_text(encoding="utf-8")
        item = load_control_evidence_registry(self.config)["evidence"][evidence_id]
        self.assertNotIn(str(source), ledger)
        self.assertNotIn("confidential test control bytes", ledger)
        self.assertEqual(item["status"], "registered")
        self.assertEqual(len(item["sha256"]), 64)
        self.assertFalse(load_control_evidence_registry(self.config)["raw_files_stored"])

    def test_future_dated_evidence_cannot_become_verified(self):
        evidence_id = self._register(
            "governance_mandate",
            "organization:test",
            valid_from="2099-01-01T00:00:00+08:00",
        )
        review_control_evidence(
            self.config,
            evidence_id=evidence_id,
            decision="approve",
            actor="human:compliance-review",
            actor_role="compliance_officer",
            notes="document reviewed but not effective yet",
            review_record_ref="records://internal/reviews/future",
        )
        item = load_control_evidence_registry(self.config)["evidence"][evidence_id]
        self.assertEqual(item["status"], "not_yet_valid")
        self.assertFalse(resolve_verified_control_evidence(self.config, evidence_id)[0])

    def test_archive_reference_rejects_local_paths_credentials_and_signed_links(self):
        for value in (
            r"C:\sensitive\contract.pdf",
            "https://user:password@archive.example.test/document/1",
            "https://archive.example.test/document/1?token=secret",
            "https://archive.example.test/document/1#temporary-access",
        ):
            with self.subTest(value=value), self.assertRaises(ControlEvidencePolicyError):
                validate_managed_archive_reference(self.config, value)
        self.assertEqual(
            validate_managed_archive_reference(self.config, "records://internal/contracts/1"),
            "records://internal/contracts/1",
        )

    def test_reviews_require_independence_and_distinct_human_actors(self):
        evidence_id = self._register(
            "governance_mandate",
            "organization:test",
            actor="human:compliance",
            actor_role="compliance_officer",
        )
        with self.assertRaisesRegex(ControlEvidencePolicyError, "independent"):
            review_control_evidence(
                self.config,
                evidence_id=evidence_id,
                decision="approve",
                actor="human:compliance",
                actor_role="compliance_officer",
                notes="self review",
                review_record_ref="records://internal/reviews/self",
            )
        dual = self._register("prohibited_actions_policy", "organization:test")
        review_control_evidence(
            self.config,
            evidence_id=dual,
            decision="approve",
            actor="human:dual-review",
            actor_role="compliance_officer",
            notes="first role review",
            review_record_ref="records://internal/reviews/dual-compliance",
        )
        with self.assertRaisesRegex(ControlEvidencePolicyError, "distinct"):
            review_control_evidence(
                self.config,
                evidence_id=dual,
                decision="approve",
                actor="human:dual-review",
                actor_role="information_security_officer",
                notes="same person second role review",
                review_record_ref="records://internal/reviews/dual-security",
            )

    def test_tamper_and_revocation_fail_closed(self):
        evidence_id = self._register_verified("provider_security", "provider_one")
        path = self.root / "data/business_onboarding/control_evidence_events.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8").replace("provider_security evidence", "tampered evidence", 1),
            encoding="utf-8",
        )
        registry = load_control_evidence_registry(self.config)
        self.assertEqual(registry["technical_status"], "FAIL")
        self.assertFalse(resolve_verified_control_evidence(self.config, evidence_id)[0])

    def test_revoked_evidence_is_immediately_unusable(self):
        evidence_id = self._register_verified("provider_security", "provider_one")
        revoke_control_evidence(
            self.config,
            evidence_id=evidence_id,
            actor="human:infosec",
            actor_role="information_security_officer",
            notes="security assurance withdrawn",
        )
        item = load_control_evidence_registry(self.config)["evidence"][evidence_id]
        self.assertEqual(item["status"], "revoked")
        self.assertFalse(resolve_verified_control_evidence(self.config, evidence_id)[0])


class BusinessOnboardingStateTests(BusinessOnboardingV29TestCase):
    def test_preflight_only_becomes_ready_after_real_control_roster_and_provider_evidence(self):
        blocked = build_business_onboarding_preflight(self.config)
        self.assertEqual(blocked["technical_status"], "READY")
        self.assertEqual(blocked["business_status"], "BLOCKED")
        self.assertIn("real legal-entity organization_id is not configured", blocked["business_blockers"])

        self._onboard_organization()
        self._onboard_required_humans()
        self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        ready = build_business_onboarding_preflight(self.config)
        self.assertEqual(ready["technical_status"], "READY")
        self.assertEqual(ready["business_status"], "READY")
        self.assertEqual([stage["phase"] for stage in ready["stages"]], PHASE_ORDER[:3])
        self.assertTrue(all(stage["status"] == "PASS" for stage in ready["stages"]))
        self.assertEqual(ready["archive_control_evidence"]["ready_count"], 3)
        self.assertFalse(ready["automatic_approval_executed"])

    def test_analyst_qualification_reference_must_match_roster(self):
        self._onboard_organization()
        self._onboard_required_humans()
        lead = next(item for item in self.principals if item["actor_id"] == "human:lead")
        lead["qualifications"]["securities_analyst_registration"]["evidence_ref"] = "control:mismatch"
        self._write_roster()
        preflight = build_business_onboarding_preflight(self.config)
        roster_stage = next(stage for stage in preflight["stages"] if stage["phase"] == "human_roster")
        self.assertEqual(roster_stage["status"], "BLOCKED")
        self.assertIn("evidence_ref mismatch", " ".join(roster_stage["blockers"]))

    def test_revoked_provider_evidence_removes_active_provider_and_blocks_stage(self):
        first = self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        self.assertEqual(load_active_provider_ids(self.config), ["provider_one", "provider_two"])
        revoke_control_evidence(
            self.config,
            evidence_id=first["provider_security"],
            actor="human:infosec",
            actor_role="information_security_officer",
            notes="provider security attestation revoked",
        )
        self.assertEqual(load_active_provider_ids(self.config), ["provider_two"])
        stage = next(
            item for item in build_business_onboarding_preflight(self.config)["stages"]
            if item["phase"] == "provider_authorization"
        )
        self.assertEqual(stage["status"], "BLOCKED")
        self.assertIn("1/2", " ".join(stage["blockers"]))

    def test_intake_pack_is_template_only_and_does_not_mutate_audit_state(self):
        event_path = self.root / "data/business_onboarding/control_evidence_events.jsonl"
        result = generate_business_onboarding_pack(self.config)
        self.assertTrue(result["template_only"])
        self.assertFalse(result["production_state_changed"])
        self.assertFalse(event_path.exists())
        self.assertEqual(len(result["files"]), 5)
        readme = json.loads(Path(result["files"][0]).read_text(encoding="utf-8"))
        self.assertEqual(readme["operating_order"], PHASE_ORDER)
        self.assertIn("No automatic", readme["safety_boundary"])

    def test_full_eight_stage_projection_can_reach_ready_only_with_current_human_go(self):
        gates = self.config["production_pilot"]["required_approval_gates"]
        self.config["business_onboarding"]["organization_id"] = "organization:test-broker-research"
        archive_evidence = {
            "control:archive-{0}".format(index): {
                "category": category,
                "subject_id": "organization:test-broker-research",
                "status": "verified",
            }
            for index, category in enumerate(
                ("production_archive_contract", "production_archive_security", "production_archive_retention"), 1
            )
        }
        preflight = {
            "technical_errors": [],
            "stages": [
                {
                    "phase": phase,
                    "owner_role": "human_owner",
                    "status": "PASS",
                    "ready_count": 1,
                    "required_count": 1,
                    "blockers": [],
                    "required_action": "none",
                    "automatic_execution": False,
                }
                for phase in PHASE_ORDER[:3]
            ],
            "control_evidence": {
                "technical_status": "READY", "integrity_status": "OK", "last_event_hash": "control-hash",
                "event_count": 10, "evidence_count": 10, "verified_evidence_ids": ["control:one"],
                "evidence": archive_evidence,
            },
            "roster": {"ready": True},
            "provider": {"active_provider_ids": ["one", "two"]},
        }
        workpapers = {
            "last_event_hash": "workpaper-hash",
            "workpapers": [{
                "workpaper_id": "wp-1",
                "case_id": "case-1",
                "case": {"priority": "P1", "entity": "测试公司", "signal_ids": ["sig-1"]},
                "source_records": [{
                    "primary_source_eligible": True,
                    "original_document_retained": True,
                    "vault_document_id": "vault-1",
                }],
            }],
        }
        pilot = {
            "technical_readiness": "READY",
            "go_live_decision": "GO",
            "candidate_fingerprint": "candidate-current",
            "shadow_evidence": {
                "minimum_runs": 5,
                "minimum_trading_days": 3,
                "qualifying_run_count": 5,
                "qualifying_trading_day_count": 3,
                "unreconciled_run_count": 0,
            },
            "requirements": {"required_approval_gates": gates},
            "approvals": {gate: {"decision": "approve"} for gate in gates},
            "activation": {"active": True},
        }
        with patch(
            "stock_agent.business_onboarding.build_business_onboarding_preflight", return_value=preflight
        ), patch(
            "stock_agent.business_onboarding.load_research_workpapers", return_value=workpapers
        ), patch(
            "stock_agent.business_onboarding.verify_research_workpapers",
            return_value={"technical_status": "READY", "production_archive_ready": True},
        ), patch(
            "stock_agent.business_onboarding.verify_evidence_vault",
            return_value={
                "technical_status": "READY", "production_archive_ready": True, "last_event_hash": "vault-hash"
            },
        ), patch(
            "stock_agent.business_onboarding.load_latest_market_data_governance", return_value={"status": "OK"}
        ), patch(
            "stock_agent.business_onboarding.load_latest_governance_summary",
            return_value={"status": "OK", "summary": {"pending_exception_count": 0}},
        ), patch("stock_agent.business_onboarding._load_latest_pilot", return_value=pilot):
            snapshot = build_business_onboarding(self.config)
        self.assertEqual(snapshot["technical_status"], "READY")
        self.assertEqual(snapshot["business_status"], "READY")
        self.assertEqual([stage["phase"] for stage in snapshot["stages"]], PHASE_ORDER)
        self.assertTrue(all(stage["status"] == "PASS" for stage in snapshot["stages"]))
        self.assertEqual(snapshot["action_count"], 0)
        self.assertFalse(snapshot["automatic_activation_executed"])
        self.assertFalse(snapshot["automatic_trading_executed"])


class IntegrationSurfaceTests(BusinessOnboardingV29TestCase):
    @staticmethod
    def _scheduled_summary(include_onboarding):
        names = [
            "market-cache-seed", "agent-run", "evidence-vault-status", "pi-handoff-run",
            "review-queue-sync", "research-ops-plan", "workspace-sync", "production-pilot-sync",
        ]
        if include_onboarding:
            names.append("business-onboarding-sync")
        names.extend(["dashboard", "alerts"])
        return {
            "agent_runtime": "pi",
            "used_pi": False,
            "agent_handoff_call_required": False,
            "agent_handoff_public_signal_count": 0,
            "pi_model_call_count": 0,
            "commands": [{"name": name} for name in names],
        }

    def test_v29_scheduled_sequence_requires_onboarding_after_pilot_before_dashboard(self):
        missing = _scheduled_sequence_status(self._scheduled_summary(False), self.config)
        self.assertFalse(missing["valid"])
        self.assertIn("business-onboarding-sync", missing["missing"])
        valid = _scheduled_sequence_status(self._scheduled_summary(True), self.config)
        self.assertTrue(valid["valid"])
        names = valid["command_names"]
        self.assertLess(names.index("production-pilot-sync"), names.index("business-onboarding-sync"))
        self.assertLess(names.index("business-onboarding-sync"), names.index("dashboard"))

    def test_control_ledger_changes_production_candidate_fingerprint_input(self):
        operations = {
            "roster_path": "config/roster.json",
            "restrictions_path": "config/restrictions.json",
        }
        registry = {"providers": {}, "active_provider_ids": []}
        before = _critical_input_hashes(self.config, "", registry, operations)
        self._register("governance_mandate", "organization:test")
        after = _critical_input_hashes(self.config, "", registry, operations)
        self.assertEqual(before["control_evidence_ledger"], "missing")
        self.assertNotEqual(after["control_evidence_ledger"], "missing")
        self.assertNotEqual(before["control_evidence_ledger"], after["control_evidence_ledger"])

    def test_protected_pilot_approver_must_be_onboarding_ready_for_the_exact_role(self):
        preflight = {
            "technical_status": "READY",
            "roster": {"eligible_humans_by_role": {"lead_analyst": ["human:lead"]}},
        }
        with patch("stock_agent.production_pilot.build_business_onboarding_preflight", return_value=preflight):
            _require_onboarding_ready_approver(self.config, "human:lead", "lead_analyst")
            with self.assertRaisesRegex(ProductionPilotPolicyError, "not onboarding-ready"):
                _require_onboarding_ready_approver(self.config, "human:quality", "lead_analyst")

    def test_doctor_and_dashboard_keep_technical_and_business_readiness_separate(self):
        blocked_snapshot = {
            "technical_status": "READY",
            "business_status": "BLOCKED",
            "current_phase": "human_roster",
            "completed_phase_count": 1,
            "phase_count": 8,
            "action_count": 7,
            "stages": [{
                "phase": "human_roster", "owner_role": "research_coordinator", "status": "BLOCKED",
                "ready_count": 0, "required_count": 7, "blockers": ["real humans missing"],
            }],
            "action_queue": [{
                "sequence": 1, "phase": "human_roster", "owner_role": "research_coordinator",
                "action": "Enter real people", "automatic_execution": False,
            }],
            "control_evidence": {"integrity_status": "OK", "evidence_count": 0, "verified_count": 0},
        }
        with patch("stock_agent.doctor.build_business_onboarding", return_value=blocked_snapshot):
            status = _check_business_onboarding(self.config)
        self.assertEqual(status[0], "WARN")
        self.assertIn("business=BLOCKED", status[1])
        html = _section_business_onboarding(blocked_snapshot)
        self.assertIn("Business Onboarding", html)
        self.assertIn("real humans missing", html)
        self.assertIn("不自动新增人员", html)
        failed = dict(blocked_snapshot, technical_status="FAIL", technical_errors=["hash chain failed"])
        with patch("stock_agent.doctor.build_business_onboarding", return_value=failed):
            self.assertEqual(_check_business_onboarding(self.config)[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
