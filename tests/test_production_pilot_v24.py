import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.audit_log import AuditLogIntegrityError, append_hash_chained_jsonl, load_hash_chained_jsonl
from stock_agent.config import project_path_override
from stock_agent.dashboard import _section_production_pilot
from stock_agent.market_data_governance import market_data_governance_config
from stock_agent.openclaw_handoff import write_openclaw_handoff
from stock_agent.production_pilot import (
    ProductionPilotPolicyError,
    _critical_input_hashes,
    _append_reconciliation_if_new,
    _shadow_evidence,
    activate_production_candidate,
    load_latest_production_pilot,
    record_pilot_approval,
    record_pilot_incident,
    record_pilot_incident_action,
    revoke_production_activation,
    sync_production_pilot,
)
from stock_agent.provider_registry import (
    DEFAULT_EVIDENCE_FIELDS,
    ProviderRegistryPolicyError,
    activate_provider,
    initialize_provider_candidate,
    load_active_provider_ids,
    load_provider_registry,
    record_provider_review,
    submit_provider_due_diligence,
    suspend_provider,
)
from stock_agent.review_workbench import render_production_pilot_workbench_html


class ProductionPilotV24TestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.override = project_path_override(lambda value: self.root / str(value))
        self.override.__enter__()
        self.config = self._config()
        self._write_json("config/roster.json", {"principals": self._principals()})
        self._write_json("config/restrictions.json", {"entries": []})
        self._write_json("config/test.json", self.config)

    def tearDown(self):
        self.override.__exit__(None, None, None)
        self.temp.cleanup()

    def _config(self):
        return {
            "research_operations": {
                "enabled": True,
                "enforce_roster": True,
                "roster_path": "config/roster.json",
                "restrictions_path": "config/restrictions.json",
            },
            "market_data_governance": {
                "enabled": True,
                "report_dir": "reports/market_data_governance",
                "use_production_pilot_provider_registry": True,
                "approved_internal_research_providers": ["unreviewed_static_provider"],
            },
            "llm_governance": {
                "enabled": True,
                "enforce": True,
                "report_path": "reports/llm_governance",
                "exception_path": "data/llm_governance/exceptions",
            },
            "model_operations": {
                "enabled": True,
                "report_dir": "reports/model_operations",
            },
            "production_pilot": {
                "enabled": True,
                "enforce": True,
                "mode": "shadow",
                "minimum_active_providers": 2,
                "minimum_shadow_runs": 1,
                "minimum_shadow_trading_days": 1,
                "maximum_unreconciled_runs": 0,
                "review_batch_size": 2,
                "review_wip_limit_per_human": 2,
                "approval_validity_days": 30,
                "vendor_event_path": "data/production_pilot/vendor_events.jsonl",
                "control_event_path": "data/production_pilot/control_events.jsonl",
                "incident_event_path": "data/production_pilot/incident_events.jsonl",
                "run_ledger_path": "data/production_pilot/run_ledger.jsonl",
                "report_dir": "reports/production_pilot",
                "required_provider_scopes": ["internal_research", "external_llm"],
                "required_provider_review_roles": ["compliance_officer", "information_security_officer"],
                "required_provider_evidence": list(DEFAULT_EVIDENCE_FIELDS),
                "required_approval_gates": {
                    "research_readiness": "lead_analyst",
                    "quality_readiness": "quality_reviewer",
                    "compliance_readiness": "compliance_officer",
                    "information_security_readiness": "information_security_officer",
                    "data_readiness": "data_steward",
                    "publication_readiness": "publication_approver",
                },
                "automatic_assignment": False,
                "automatic_approval": False,
                "automatic_activation": False,
                "automatic_publication": False,
                "automatic_trading": False,
            },
        }

    def _principals(self):
        return [
            self._human("human:coord", ["research_coordinator"]),
            self._human("human:lead", ["lead_analyst"], analyst=True),
            self._human("human:quality", ["quality_reviewer"], analyst=True),
            self._human("human:compliance", ["compliance_officer"], authorizations=["compliance_authorized"]),
            self._human("human:infosec", ["information_security_officer"], authorizations=["information_security_authorized"]),
            self._human("human:data", ["data_steward"], authorizations=["vendor_data_steward_authorized"]),
            self._human("human:publisher", ["publication_approver"], authorizations=["publication_authorized"]),
        ]

    def _human(self, actor_id, roles, analyst=False, authorizations=None):
        qualifications = {}
        if analyst:
            qualifications = {
                "securities_analyst_registration": {
                    "status": "verified",
                    "registration_id": "TEST-{0}".format(actor_id.split(":")[-1]),
                }
            }
        return {
            "actor_id": actor_id,
            "display_name": actor_id,
            "principal_type": "human",
            "active": True,
            "roles": roles,
            "information_side": "public",
            "qualifications": qualifications,
            "authorizations": list(authorizations or []),
            "coverage_entities": ["*"],
            "restricted_entities": [],
            "max_active_cases": 10,
        }

    def _write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_workpaper_policy_is_candidate_fingerprinted_but_events_are_not(self):
        config = json.loads(json.dumps(self.config))
        config["research_workpapers"] = {
            "enabled": True,
            "enforce": True,
            "retention_years": 5,
            "require_official_primary_for": ["P0", "P1"],
            "require_exact_evidence_refs": True,
            "require_market_governance_pass": True,
            "require_distinct_human_roles": True,
            "require_independent_challenge": True,
            "production_archive_approved": False,
            "production_archive_evidence_ref": "",
        }
        operations = {
            "roster_path": "config/roster.json",
            "restrictions_path": "config/restrictions.json",
        }
        registry = {"providers": [], "active_provider_ids": []}
        first = _critical_input_hashes(config, "config/test.json", registry, operations)

        event_path = self.root / "data" / "research_workpapers" / "events.jsonl"
        event_path.parent.mkdir(parents=True)
        event_path.write_text('{"event":"draft"}\n', encoding="utf-8")
        after_event = _critical_input_hashes(config, "config/test.json", registry, operations)
        self.assertEqual(first["critical_config"], after_event["critical_config"])

        weakened = json.loads(json.dumps(config))
        weakened["research_workpapers"]["require_independent_challenge"] = False
        after_policy_change = _critical_input_hashes(weakened, "config/test.json", registry, operations)
        self.assertNotEqual(first["critical_config"], after_policy_change["critical_config"])

    def _evidence(self, prefix="evidence"):
        return {field: "{0}/{1}".format(prefix, field) for field in DEFAULT_EVIDENCE_FIELDS}

    def _onboard_provider(self, provider_id):
        initialize_provider_candidate(
            self.config,
            provider_id,
            "{0} Legal Entity".format(provider_id),
            ["internal_research", "external_llm"],
        )
        submit_provider_due_diligence(
            self.config,
            provider_id,
            "human:data",
            "data_steward",
            self._evidence(provider_id),
            notes="complete due diligence",
        )
        record_provider_review(
            self.config,
            provider_id,
            "approve",
            "human:compliance",
            "compliance_officer",
            notes="data rights and compliance reviewed",
            evidence_refs=["review/compliance/{0}".format(provider_id)],
        )
        record_provider_review(
            self.config,
            provider_id,
            "approve",
            "human:infosec",
            "information_security_officer",
            notes="security and continuity reviewed",
            evidence_refs=["review/security/{0}".format(provider_id)],
        )
        return activate_provider(
            self.config,
            provider_id,
            "human:data",
            "data_steward",
            notes="manual activation after independent reviews",
        )

    def _write_operational_artifacts(self):
        stem = "20260818T083000_0800"
        self._write_json("reports/json/{0}.json".format(stem), {"generated_at": "2026-08-18T08:30:00+08:00", "stats": {}, "signals": []})
        for relative, text in (
            ("reports/daily/{0}.md".format(stem), "# daily\n"),
            ("reports/alerts/{0}.jsonl".format(stem), ""),
            ("reports/dashboard/latest.html", "<html></html>"),
            ("reports/notifications/latest.md", "# notifications\n"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self._write_json("logs/run_ops/latest.json", {"run_id": stem, "overall_status": "OK"})
        self._write_json("reports/market_data_governance/latest.json", {
            "status": "OK", "pass_count": 2, "blocked_count": 0, "llm_eligible_count": 2,
        })
        self._write_json("reports/llm_governance/latest.json", {
            "status": "OK", "summary": {"denied_count": 0, "output_blocked_count": 0, "pending_exception_count": 0},
        })
        self._write_json("reports/model_operations/latest.json", {
            "status": "PASS", "run_id": stem, "breach_count": 0,
            "checks": [{"name": "quality", "status": "PASS"}],
            "rolling": {"total_tokens": 1000}, "calls": [],
        })
        self._write_json("reports/review_queue/latest.json", {"summary": {}, "items": []})
        self._write_json("reports/research_runtime/latest.json", {"summary": {}, "runtime": {"cases": []}})
        self._write_json("reports/research_operations/latest.json", {"summary": {}})
        self._write_json("reports/workspace/latest.json", {"summary": {}})
        self._write_json("reports/health/latest.json", {"overall_status": "WARN", "checks": []})
        write_openclaw_handoff(self.config)
        self._write_json("logs/scheduled/run_20260818T083000.summary.json", {
            "mode": "daily",
            "trigger_source": "scheduled",
            "exit_code": 0,
            "report_path": str(self.root / "reports/json/{0}.json".format(stem)),
            "started_at": "2026-08-18T08:30:00+08:00",
            "finished_at": "2026-08-18T08:34:00+08:00",
            "used_openclaw": False,
            "openclaw_handoff_public_signal_count": 0,
            "openclaw_handoff_call_required": False,
            "commands": [
                {"name": "market-cache-seed"},
                {"name": "agent-run"},
                {"name": "evidence-vault-status"},
                {"name": "openclaw-handoff"},
                {"name": "review-queue-sync"},
                {"name": "research-ops-plan"},
                {"name": "workspace-sync"},
                {"name": "production-pilot-sync"},
                {"name": "dashboard"},
                {"name": "alerts"},
            ],
        })

    def _ready_candidate(self):
        self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        self._write_operational_artifacts()
        first = sync_production_pilot(self.config, config_path="config/test.json")
        self.assertEqual(first["shadow_evidence"]["qualifying_run_count"], 1)
        approvals = [
            ("research_readiness", "human:lead", "lead_analyst"),
            ("quality_readiness", "human:quality", "quality_reviewer"),
            ("compliance_readiness", "human:compliance", "compliance_officer"),
            ("information_security_readiness", "human:infosec", "information_security_officer"),
            ("data_readiness", "human:data", "data_steward"),
            ("publication_readiness", "human:publisher", "publication_approver"),
        ]
        current = first
        for gate, actor, role in approvals:
            current = record_pilot_approval(
                self.config,
                gate,
                "approve",
                actor,
                role,
                notes="verified readiness for {0}".format(gate),
                evidence_refs=["acceptance/{0}".format(gate)],
                config_path="config/test.json",
            )["pilot"]
        return current


class AuditLogTests(ProductionPilotV24TestCase):
    def test_hash_chain_detects_tamper_and_blocks_append(self):
        path = self.root / "data/audit/events.jsonl"
        append_hash_chained_jsonl(path, {"event_type": "one"})
        append_hash_chained_jsonl(path, {"event_type": "two"})
        self.assertEqual(load_hash_chained_jsonl(path)["integrity_status"], "OK")
        text = path.read_text(encoding="utf-8").replace('"event_type": "one"', '"event_type": "tampered"', 1)
        path.write_text(text, encoding="utf-8")
        self.assertEqual(load_hash_chained_jsonl(path)["integrity_status"], "FAIL")
        with self.assertRaises(AuditLogIntegrityError):
            append_hash_chained_jsonl(path, {"event_type": "three"})


class ProviderLifecycleTests(ProductionPilotV24TestCase):
    def test_full_provider_lifecycle_requires_two_independent_reviews(self):
        self._onboard_provider("provider_one")
        registry = load_provider_registry(self.config)
        self.assertEqual(registry["providers"]["provider_one"]["status"], "active")
        self.assertEqual(load_active_provider_ids(self.config, "external_llm"), ["provider_one"])
        market_cfg = market_data_governance_config(self.config)
        self.assertEqual(market_cfg["approved_internal_research_providers"], ["provider_one"])
        self.assertNotIn("unreviewed_static_provider", market_cfg["approved_internal_research_providers"])

    def test_missing_evidence_and_service_or_unregistered_actors_are_rejected(self):
        initialize_provider_candidate(self.config, "provider_one", "Provider One", ["internal_research", "external_llm"])
        with self.assertRaises(ProviderRegistryPolicyError):
            submit_provider_due_diligence(
                self.config, "provider_one", "human:data", "data_steward", {}, notes="missing evidence",
            )
        with self.assertRaises(ProviderRegistryPolicyError):
            submit_provider_due_diligence(
                self.config, "provider_one", "system:stock_agent", "data_steward", self._evidence(), notes="invalid actor",
            )

    def test_reviewer_cannot_be_submitter_and_reviewers_must_be_distinct(self):
        principals = self._principals()
        for item in principals:
            if item["actor_id"] == "human:data":
                item["roles"].append("compliance_officer")
                item["authorizations"].append("compliance_authorized")
        self._write_json("config/roster.json", {"principals": principals})
        initialize_provider_candidate(self.config, "provider_one", "Provider One", ["internal_research", "external_llm"])
        submit_provider_due_diligence(
            self.config, "provider_one", "human:data", "data_steward", self._evidence(), notes="complete",
        )
        with self.assertRaises(ProviderRegistryPolicyError):
            record_provider_review(
                self.config, "provider_one", "approve", "human:data", "compliance_officer",
                notes="self review", evidence_refs=["review/self"],
            )

    def test_suspension_immediately_removes_provider_from_active_scope(self):
        self._onboard_provider("provider_one")
        suspend_provider(
            self.config, "provider_one", "human:compliance", "compliance_officer", notes="rights under investigation",
        )
        self.assertEqual(load_active_provider_ids(self.config, "external_llm"), [])
        self.assertEqual(load_provider_registry(self.config)["providers"]["provider_one"]["status"], "suspended")

    def test_registry_tamper_fails_closed(self):
        self._onboard_provider("provider_one")
        path = self.root / "data/production_pilot/vendor_events.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("provider_one", "provider_bad", 1), encoding="utf-8")
        registry = load_provider_registry(self.config)
        self.assertEqual(registry["integrity_status"], "FAIL")
        self.assertEqual(registry["active_provider_ids"], [])


class PilotGateTests(ProductionPilotV24TestCase):
    def test_current_controls_are_no_go_until_real_gates_complete(self):
        self._write_operational_artifacts()
        snapshot = sync_production_pilot(self.config, config_path="config/test.json")
        self.assertEqual(snapshot["go_live_decision"], "NO_GO")
        self.assertFalse(snapshot["candidate_ready_for_activation"])
        self.assertFalse(snapshot["automatic_approval_executed"])
        self.assertFalse(snapshot["automatic_trading_executed"])

    def test_full_realistic_sequence_reaches_internal_research_go(self):
        candidate = self._ready_candidate()
        self.assertEqual(candidate["candidate_readiness"], "READY_FOR_HUMAN_ACTIVATION")
        self.assertEqual(candidate["go_live_decision"], "NO_GO")
        result = activate_production_candidate(
            self.config,
            "human:publisher",
            "publication_approver",
            notes="manual internal research activation",
            evidence_refs=["acceptance/final"],
            config_path="config/test.json",
        )
        self.assertEqual(result["pilot"]["go_live_decision"], "GO")
        self.assertEqual(result["pilot"]["effective_mode"], "production_internal_research")
        self.assertFalse(result["pilot"]["automatic_trading_executed"])

    def test_run_ledger_is_idempotent_for_same_report_and_candidate(self):
        self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        self._write_operational_artifacts()
        sync_production_pilot(self.config, config_path="config/test.json")
        sync_production_pilot(self.config, config_path="config/test.json")
        ledger = load_hash_chained_jsonl(self.root / "data/production_pilot/run_ledger.jsonl")
        self.assertEqual(ledger["record_count"], 1)

    def test_reconciliation_correction_is_appended_and_latest_state_is_projected(self):
        cfg = self.config["production_pilot"]
        common = {
            "event_type": "shadow_run_reconciled",
            "recorded_at": "2026-08-23T08:45:00+08:00",
            "run_identity": "legacy-changing-hash",
            "candidate_fingerprint": "candidate-a",
            "report_generated_at": "2026-08-23T08:30:00+08:00",
            "run_id": "daily-20260823",
            "scheduled_summary_path": str(self.root / "logs/scheduled/run_daily.summary.json"),
            "scheduled_report_path": str(self.root / "reports/json/daily.json"),
            "artifact_hashes": {},
            "missing_artifacts": [],
            "stale_artifacts": [],
            "scheduled_report_match": True,
            "scheduled_handoff_match": True,
            "qualifying_shadow_run": False,
            "qualification_reasons": ["artifacts are not reconciled"],
        }
        failed = dict(common, hash_errors=[{"artifact": "pi_result", "error_type": "handoff_hash_mismatch"}], reconciled=False)
        corrected = dict(common, recorded_at="2026-08-23T08:46:00+08:00", hash_errors=[], reconciled=True)
        _append_reconciliation_if_new(cfg, failed)
        _append_reconciliation_if_new(cfg, corrected)
        _append_reconciliation_if_new(cfg, corrected)
        ledger = load_hash_chained_jsonl(self.root / "data/production_pilot/run_ledger.jsonl")
        self.assertEqual(ledger["record_count"], 2)
        self.assertEqual(ledger["records"][1]["reconciliation_revision"], 2)
        self.assertEqual(ledger["records"][1]["supersedes_event_id"], ledger["records"][0]["event_id"])
        projection = _shadow_evidence(ledger, cfg, "candidate-a")
        self.assertEqual(projection["recorded_run_count"], 1)
        self.assertEqual(projection["raw_reconciliation_event_count"], 2)
        self.assertEqual(projection["superseded_reconciliation_event_count"], 1)
        self.assertEqual(projection["historical_unreconciled_event_count"], 1)
        self.assertEqual(projection["unreconciled_run_count"], 0)
        self.assertTrue(projection["latest_run"]["reconciled"])

    def test_legacy_reconciliation_events_project_by_stable_run_key(self):
        common = {
            "event_type": "shadow_run_reconciled",
            "candidate_fingerprint": "candidate-a",
            "run_id": "daily-20260823",
            "scheduled_summary_path": "logs/scheduled/run_daily.summary.json",
            "scheduled_report_path": "reports/json/daily.json",
            "artifact_hashes": {},
            "qualifying_shadow_run": False,
        }
        failed = dict(common, sequence=1, run_identity="old-handoff-hash-a", reconciled=False)
        corrected = dict(common, sequence=2, run_identity="old-handoff-hash-b", reconciled=True)
        projection = _shadow_evidence(
            {"integrity_status": "OK", "records": [failed, corrected]},
            self.config["production_pilot"],
            "candidate-a",
        )
        self.assertEqual(projection["recorded_run_count"], 1)
        self.assertEqual(projection["unreconciled_run_count"], 0)
        self.assertEqual(projection["historical_unreconciled_event_count"], 1)

    def test_old_scheduled_summary_cannot_be_paired_with_new_report(self):
        self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        self._write_operational_artifacts()
        summary_path = self.root / "logs/scheduled/run_20260818T083000.summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["report_path"] = str(self.root / "reports/json/older_report.json")
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        snapshot = sync_production_pilot(self.config, config_path="config/test.json")
        self.assertFalse(snapshot["reconciliation"]["scheduled_report_match"])
        self.assertEqual(snapshot["shadow_evidence"]["recorded_run_count"], 0)

    def test_shadow_evidence_is_scoped_to_current_candidate_fingerprint(self):
        self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        self._write_operational_artifacts()
        first = sync_production_pilot(self.config, config_path="config/test.json")
        self.assertEqual(first["shadow_evidence"]["qualifying_run_count"], 1)
        self.config["production_pilot"]["review_batch_size"] = 3
        self._write_json("config/test.json", self.config)
        second = sync_production_pilot(self.config, config_path="config/test.json")
        ledger = load_hash_chained_jsonl(self.root / "data/production_pilot/run_ledger.jsonl")
        self.assertEqual(ledger["record_count"], 2)
        self.assertEqual(second["shadow_evidence"]["qualifying_run_count"], 1)

    def test_distinct_actor_rule_blocks_one_person_approving_two_gates(self):
        principals = self._principals()
        for item in principals:
            if item["actor_id"] == "human:lead":
                item["roles"].append("quality_reviewer")
        self._write_json("config/roster.json", {"principals": principals})
        self._onboard_provider("provider_one")
        self._onboard_provider("provider_two")
        self._write_operational_artifacts()
        sync_production_pilot(self.config, config_path="config/test.json")
        record_pilot_approval(
            self.config, "research_readiness", "approve", "human:lead", "lead_analyst",
            notes="research ready", evidence_refs=["acceptance/research"], config_path="config/test.json",
        )
        with self.assertRaises(ProductionPilotPolicyError):
            record_pilot_approval(
                self.config, "quality_readiness", "approve", "human:lead", "quality_reviewer",
                notes="self quality", evidence_refs=["acceptance/quality"], config_path="config/test.json",
            )

    def test_config_change_invalidates_existing_approvals_and_activation(self):
        self._ready_candidate()
        activated = activate_production_candidate(
            self.config, "human:publisher", "publication_approver",
            notes="activate", evidence_refs=["acceptance/final"], config_path="config/test.json",
        )["pilot"]
        old_fingerprint = activated["candidate_fingerprint"]
        self.config["production_pilot"]["review_batch_size"] = 3
        self._write_json("config/test.json", self.config)
        changed = sync_production_pilot(self.config, config_path="config/test.json", record_run=False)
        self.assertNotEqual(changed["candidate_fingerprint"], old_fingerprint)
        self.assertEqual(changed["approvals"], {})
        self.assertEqual(changed["go_live_decision"], "NO_GO")
        self.assertEqual(changed["effective_mode"], "shadow")

    def test_revocation_fails_closed_without_touching_research_artifacts(self):
        self._ready_candidate()
        activate_production_candidate(
            self.config, "human:publisher", "publication_approver",
            notes="activate", evidence_refs=["acceptance/final"], config_path="config/test.json",
        )
        report = self.root / "reports/json/20260818T083000_0800.json"
        before = report.read_bytes()
        result = revoke_production_activation(
            self.config, "human:compliance", "compliance_officer",
            notes="control investigation", config_path="config/test.json",
        )
        self.assertEqual(result["pilot"]["go_live_decision"], "NO_GO")
        self.assertEqual(result["pilot"]["effective_mode"], "shadow")
        self.assertEqual(report.read_bytes(), before)

    def test_unsafe_automatic_flag_blocks_technical_readiness(self):
        self._write_operational_artifacts()
        self.config["production_pilot"]["automatic_approval"] = True
        self._write_json("config/test.json", self.config)
        snapshot = sync_production_pilot(self.config, config_path="config/test.json")
        self.assertEqual(snapshot["technical_readiness"], "BLOCKED")
        self.assertEqual(next(item for item in snapshot["gates"] if item["gate"] == "safety_policy")["status"], "BLOCKED")


class IncidentAndSurfaceTests(ProductionPilotV24TestCase):
    def test_critical_incident_requires_two_distinct_human_resolution_roles(self):
        record = record_pilot_incident(
            self.config,
            "quote provenance mismatch",
            "critical",
            "market_data",
            "system:stock_agent",
            "system_monitor",
            notes="provider timestamps diverged",
            evidence_refs=["reports/market_data_governance/latest.json"],
        )["record"]
        incident_id = record["incident_id"]
        first = record_pilot_incident_action(
            self.config, incident_id, "resolve", "human:compliance", "compliance_officer",
            notes="rights and disclosure checked", evidence_refs=["incident/compliance"],
        )["incidents"]
        self.assertEqual(first["incidents"][incident_id]["status"], "resolution_pending_dual_review")
        second = record_pilot_incident_action(
            self.config, incident_id, "resolve", "human:infosec", "information_security_officer",
            notes="technical root cause and controls verified", evidence_refs=["incident/security"],
        )["incidents"]
        self.assertEqual(second["incidents"][incident_id]["status"], "resolved")

    def test_unregistered_incident_reporter_is_rejected(self):
        with self.assertRaises(ProductionPilotPolicyError):
            record_pilot_incident(
                self.config, "event", "medium", "ops", "unknown-service", "monitor",
                notes="details", evidence_refs=["incident/evidence"],
            )

    def test_dashboard_and_workbench_show_separate_readiness_states(self):
        self._write_operational_artifacts()
        snapshot = sync_production_pilot(self.config, config_path="config/test.json")
        dashboard = _section_production_pilot(snapshot)
        workbench = render_production_pilot_workbench_html(config=self.config)
        self.assertIn("NO_GO", dashboard)
        self.assertIn("技术", dashboard)
        self.assertIn("Controlled Production Pilot", workbench)
        self.assertIn("Go/No-Go Gates", workbench)
        self.assertNotIn("automatic trading", workbench.lower())

    def test_latest_snapshot_round_trips(self):
        self._write_operational_artifacts()
        written = sync_production_pilot(self.config, config_path="config/test.json")
        loaded = load_latest_production_pilot(self.config)
        self.assertEqual(loaded["candidate_fingerprint"], written["candidate_fingerprint"])
        self.assertTrue((self.root / "reports/production_pilot/latest.md").exists())


if __name__ == "__main__":
    unittest.main()
