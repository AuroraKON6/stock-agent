from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.value_validation import (
    ValueValidationPolicyError,
    build_value_validation_status,
    freeze_value_cohort,
    freeze_value_treatment,
    open_value_study,
    prepare_blind_assignment,
    record_blind_evaluation,
    record_value_assisted,
    record_value_baseline,
    record_value_gate_decision,
    register_value_case,
    reveal_blind_assignment,
    write_value_validation_status,
)


class ValueValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_id = "a" * 64
        self.config = {
            "value_validation": {
                "enabled": True,
                "study_id": "value-study-1",
                "event_path": "data/value_validation/events.jsonl",
                "sealed_dir": "data/value_validation/sealed",
                "report_dir": "reports/value_validation",
                "blind_report_dir": "reports/value_validation/blind_assignments",
                "minimum_cases": 2,
                "maximum_cases": 3,
                "minimum_strata": 2,
                "target_efficiency_improvement": 0.2,
                "quality_non_inferiority_margin": 0,
                "minimum_blind_coverage": 1.0,
                "maximum_unauthorized_egress": 0,
                "require_same_analyst": True,
                "allowed_priorities": ["P0", "P1"],
                "allowed_data_classifications": ["public"],
                "qualifying_measurement_methods": ["time_log"],
                "release_audit_path": "reports/release_audit/latest.json",
            },
            "research_operations": {
                "enabled": True,
                "enforce_roster": True,
                "roster_path": "config/roster.json",
                "restrictions_path": "config/restrictions.json",
            },
        }
        self._write_json("config/roster.json", {"principals": self._principals()})
        self._write_json("config/restrictions.json", {"entries": []})
        self._write_json("reports/release_audit/latest.json", self._ready_release())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _principals(self):
        registration = {
            "securities_analyst_registration": {
                "status": "verified",
                "registration_id": "TEST-REGISTRATION",
            }
        }
        common = {
            "principal_type": "human",
            "active": True,
            "information_side": "public",
            "authorizations": [],
            "coverage_entities": ["*"],
            "restricted_entities": [],
            "max_active_cases": 20,
        }
        return [
            dict(common, actor_id="human:coord", roles=["research_coordinator"]),
            dict(
                common,
                actor_id="human:supervisor",
                roles=["research_supervisor"],
                authorizations=["publication_authorized"],
            ),
            dict(
                common,
                actor_id="human:analyst",
                roles=["lead_analyst", "quality_reviewer"],
                qualifications=registration,
            ),
            dict(
                common,
                actor_id="human:reviewer",
                roles=["quality_reviewer"],
                qualifications=registration,
            ),
            {
                "actor_id": "service:worker",
                "principal_type": "service",
                "active": True,
                "roles": ["information_analyst"],
                "information_side": "public",
                "coverage_entities": ["*"],
                "max_active_cases": 20,
            },
        ]

    def _ready_release(self, ready=True):
        return {
            "status": "READY" if ready else "BLOCKED",
            "technical_release_ready": bool(ready),
            "manifest_id": self.manifest_id,
            "identity_projection": {
                "release_candidate_fingerprint": "b" * 64,
                "models": {
                    "llm_runtime": "pi",
                    "llm_model": "deepseek-v4-pro",
                    "pi_provider": "deepseek",
                    "pi_model": "deepseek-v4-pro",
                },
                "pi": {"skill_bundle_sha256": "c" * 64},
            },
        }

    def _write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _call(self, function, *args, **kwargs):
        with project_path_override(lambda value: self.root / str(value)):
            return function(*args, **kwargs)

    def _status(self):
        return self._call(build_value_validation_status, self.config)

    def _open(self):
        return self._call(
            open_value_study,
            self.config,
            title="P0/P1 event triage value test",
            scenario="Public filing and market-event research",
            actor="human:coord",
            actor_role="research_coordinator",
        )

    def _register_cases(self):
        for index, stratum in enumerate(("filing", "market"), 1):
            self._call(
                register_value_case,
                self.config,
                case_id="case-{0}".format(index),
                case_ref="archive:case-{0}".format(index),
                case_sha256=(str(index) * 64),
                priority="P1" if index == 1 else "P0",
                stratum=stratum,
                evidence_cutoff="2026-08-01T09:00:00+08:00",
                data_classification="public",
                actor="human:coord",
                actor_role="research_coordinator",
            )

    def _freeze(self):
        return self._call(
            freeze_value_cohort,
            self.config,
            actor="human:supervisor",
            actor_role="research_supervisor",
        )

    @staticmethod
    def _metrics(active=120, assisted=False, method="time_log"):
        segments = {
            "collection": active * 0.2,
            "evidence": active * 0.2,
            "analysis": active * 0.25,
            "drafting": active * 0.2,
            "review": active * 0.1,
            "correction": active * 0.05,
        }
        value = {
            "started_at": "2026-08-01T09:00:00+08:00",
            "completed_at": "2026-08-01T12:00:00+08:00",
            "active_minutes": active,
            "segment_minutes": segments,
            "measurement_method": method,
            "measurement_evidence_ref": "archive:opaque-time-log",
            "measurement_evidence_sha256": "9" * 64,
            "tool_switch_count": 6,
            "source_count": 8,
            "citation_count": 6,
        }
        if assisted:
            value.update({
                "system_duration_seconds": 50,
                "prompt_tokens": 1000,
                "completion_tokens": 300,
                "reasoning_tokens": 200,
                "estimated_cost_cny": 0.5,
                "system_run_ref": "report:opaque-run-summary",
                "system_run_sha256": "8" * 64,
            })
        return value

    @staticmethod
    def _scores(score, defects=0):
        return {
            "evidence_quality": score,
            "analytical_soundness": score,
            "completeness": score,
            "actionability": score,
            "clarity": score,
            "citation_accuracy": score,
            "material_errors": defects,
            "material_omissions": defects,
            "unsupported_claims": defects,
        }

    def _record_baselines(self):
        for index in (1, 2):
            self._call(
                record_value_baseline,
                self.config,
                case_id="case-{0}".format(index),
                output_ref="archive:opaque-x{0}".format(index),
                output_sha256=("d" if index == 1 else "e") * 64,
                metrics=self._metrics(120),
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def _freeze_treatment(self):
        return self._call(
            freeze_value_treatment,
            self.config,
            actor="human:supervisor",
            actor_role="research_supervisor",
        )

    def _record_assisted(self, manifest_id=None, unauthorized=0, method="time_log"):
        for index in (1, 2):
            self._call(
                record_value_assisted,
                self.config,
                case_id="case-{0}".format(index),
                output_ref="archive:opaque-y{0}".format(index),
                output_sha256=("f" if index == 1 else "0") * 64,
                metrics=self._metrics(80, assisted=True, method=method),
                observed_manifest_id=manifest_id or self.manifest_id,
                outcome_hidden=True,
                evidence_cutoff_respected=True,
                unauthorized_egress_count=unauthorized,
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def _blind_all(self, assisted_score=90, baseline_score=80):
        for index in (1, 2):
            prepared = self._call(
                prepare_blind_assignment,
                self.config,
                case_id="case-{0}".format(index),
                actor="human:coord",
                actor_role="research_coordinator",
                mapping={"A": "baseline", "B": "assisted"},
                nonce="nonce-{0}".format(index),
            )
            self._call(
                record_blind_evaluation,
                self.config,
                assignment_id=prepared["assignment_id"],
                scores_a=self._scores(baseline_score, defects=1),
                scores_b=self._scores(assisted_score, defects=0),
                blinding_attested=True,
                no_ledger_access_attested=True,
                notes="Independent blind review completed.",
                actor="human:reviewer",
                actor_role="quality_reviewer",
            )
            self._call(
                reveal_blind_assignment,
                self.config,
                assignment_id=prepared["assignment_id"],
                actor="human:coord",
                actor_role="research_coordinator",
            )

    def _complete_study(self, assisted_score=90):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted()
        self._blind_all(assisted_score=assisted_score)

    def test_empty_enabled_study_is_not_started_and_no_go(self):
        status = self._status()
        self.assertEqual(status["operating_status"], "NOT_STARTED")
        self.assertEqual(status["recommended_decision"], "INSUFFICIENT_EVIDENCE")
        self.assertFalse(status["operation_contract"]["automatic_gate_decision"])

    def test_service_actor_cannot_open_study(self):
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                open_value_study,
                self.config,
                title="Real study",
                scenario="Real scenario",
                actor="service:worker",
                actor_role="research_coordinator",
            )

    def test_case_scope_and_duplicates_are_rejected(self):
        self._open()
        self._call(
            register_value_case,
            self.config,
            case_id="case-1",
            case_ref="archive:case-1",
            case_sha256="1" * 64,
            priority="P1",
            stratum="filing",
            evidence_cutoff="2026-08-01T09:00:00+08:00",
            data_classification="public",
            actor="human:coord",
            actor_role="research_coordinator",
        )
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                register_value_case,
                self.config,
                case_id="case-1",
                case_ref="archive:case-1",
                case_sha256="1" * 64,
                priority="P1",
                stratum="filing",
                evidence_cutoff="2026-08-01T09:00:00+08:00",
                data_classification="public",
                actor="human:coord",
                actor_role="research_coordinator",
            )

    def test_cohort_requires_minimum_cases_and_strata(self):
        self._open()
        with self.assertRaises(ValueValidationPolicyError):
            self._freeze()
        self._register_cases()
        event = self._freeze()
        self.assertEqual(event["payload"]["case_count"], 2)
        self.assertEqual(len(event["payload"]["case_set_fingerprint"]), 64)

    def test_observation_reconciles_active_and_segment_minutes(self):
        self._open()
        self._register_cases()
        self._freeze()
        metrics = self._metrics(120)
        metrics["segment_minutes"]["analysis"] += 20
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_value_baseline,
                self.config,
                case_id="case-1",
                output_ref="archive:opaque-x1",
                output_sha256="d" * 64,
                metrics=metrics,
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def test_example_metrics_cannot_be_submitted_as_evidence(self):
        self._open()
        self._register_cases()
        self._freeze()
        metrics = self._metrics(120)
        metrics["template_only"] = True
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_value_baseline,
                self.config,
                case_id="case-1",
                output_ref="archive:opaque-x1",
                output_sha256="d" * 64,
                metrics=metrics,
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def test_measurement_evidence_is_required(self):
        self._open()
        self._register_cases()
        self._freeze()
        metrics = self._metrics(120)
        metrics.pop("measurement_evidence_ref")
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_value_baseline,
                self.config,
                case_id="case-1",
                output_ref="archive:opaque-x1",
                output_sha256="d" * 64,
                metrics=metrics,
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def test_assisted_output_must_differ_from_baseline(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_value_assisted,
                self.config,
                case_id="case-1",
                output_ref="archive:opaque-x1",
                output_sha256="d" * 64,
                metrics=self._metrics(80, assisted=True),
                observed_manifest_id=self.manifest_id,
                outcome_hidden=True,
                evidence_cutoff_respected=True,
                unauthorized_egress_count=0,
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def test_output_reference_must_not_reveal_variant(self):
        self._open()
        self._register_cases()
        self._freeze()
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_value_baseline,
                self.config,
                case_id="case-1",
                output_ref="archive:baseline-result",
                output_sha256="d" * 64,
                metrics=self._metrics(),
                actor="human:analyst",
                actor_role="lead_analyst",
            )

    def test_treatment_freeze_requires_ready_g0_manifest(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._write_json("reports/release_audit/latest.json", self._ready_release(False))
        with self.assertRaises(ValueValidationPolicyError):
            self._freeze_treatment()

    def test_policy_drift_blocks_treatment_freeze(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self.config["value_validation"]["target_efficiency_improvement"] = 0.3
        with self.assertRaises(ValueValidationPolicyError):
            self._freeze_treatment()

    def test_blind_package_omits_mapping_and_sealed_data(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted()
        result = self._call(
            prepare_blind_assignment,
            self.config,
            case_id="case-1",
            actor="human:coord",
            actor_role="research_coordinator",
            mapping={"A": "baseline", "B": "assisted"},
            nonce="secret-nonce",
        )
        package_text = Path(result["package_path"]).read_text(encoding="utf-8")
        event_text = json.dumps(result["event"])
        self.assertNotIn("secret-nonce", package_text)
        self.assertNotIn('"mapping"', package_text)
        self.assertNotIn("secret-nonce", event_text)
        self.assertNotIn('"mapping"', event_text)

    def test_blind_reviewer_must_be_independent(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted()
        prepared = self._call(
            prepare_blind_assignment,
            self.config,
            case_id="case-1",
            actor="human:coord",
            actor_role="research_coordinator",
            mapping={"A": "baseline", "B": "assisted"},
            nonce="nonce",
        )
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_blind_evaluation,
                self.config,
                assignment_id=prepared["assignment_id"],
                scores_a=self._scores(80),
                scores_b=self._scores(90),
                blinding_attested=True,
                no_ledger_access_attested=True,
                notes="",
                actor="human:analyst",
                actor_role="quality_reviewer",
            )

    def test_tampered_sealed_mapping_cannot_be_revealed(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted()
        prepared = self._call(
            prepare_blind_assignment,
            self.config,
            case_id="case-1",
            actor="human:coord",
            actor_role="research_coordinator",
            mapping={"A": "baseline", "B": "assisted"},
            nonce="nonce",
        )
        self._call(
            record_blind_evaluation,
            self.config,
            assignment_id=prepared["assignment_id"],
            scores_a=self._scores(80),
            scores_b=self._scores(90),
            blinding_attested=True,
            no_ledger_access_attested=True,
            notes="",
            actor="human:reviewer",
            actor_role="quality_reviewer",
        )
        sealed = json.loads(Path(prepared["sealed_mapping_path"]).read_text(encoding="utf-8"))
        sealed["nonce"] = "tampered"
        Path(prepared["sealed_mapping_path"]).write_text(json.dumps(sealed), encoding="utf-8")
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                reveal_blind_assignment,
                self.config,
                assignment_id=prepared["assignment_id"],
                actor="human:coord",
                actor_role="research_coordinator",
            )

    def test_complete_paired_study_passes_value_gate(self):
        self._complete_study()
        status = self._status()
        self.assertEqual(status["operating_status"], "READY_FOR_DECISION")
        self.assertTrue(status["value_gate_ready"])
        self.assertEqual(status["recommended_decision"], "CONTINUE")
        self.assertEqual(status["aggregate"]["qualifying_case_count"], 2)
        self.assertGreaterEqual(status["aggregate"]["median_efficiency_improvement"], 0.2)
        self.assertGreater(status["aggregate"]["quality_delta"], 0)

    def test_manifest_mismatch_is_protocol_deviation(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted(manifest_id="9" * 64)
        self._blind_all()
        status = self._status()
        self.assertFalse(status["value_gate_ready"])
        self.assertEqual(status["aggregate"]["protocol_deviation_case_count"], 2)

    def test_unauthorized_egress_recommends_stop(self):
        self._open()
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted(unauthorized=1)
        self._blind_all()
        status = self._status()
        self.assertEqual(status["recommended_decision"], "STOP")
        self.assertEqual(status["aggregate"]["unauthorized_egress_count"], 2)

    def test_quality_regression_fails_non_inferiority(self):
        self._complete_study(assisted_score=60)
        status = self._status()
        gate = next(item for item in status["gates"] if item["gate"] == "quality_non_inferiority")
        self.assertEqual(gate["status"], "FAIL")
        self.assertEqual(status["recommended_decision"], "NARROW_OR_STOP")

    def test_continue_decision_requires_all_gates_and_is_human_only(self):
        self._open()
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                record_value_gate_decision,
                self.config,
                decision="CONTINUE",
                notes="Continue based on evidence.",
                evidence_ref="archive:value-pack",
                actor="human:supervisor",
                actor_role="research_supervisor",
            )
        self._register_cases()
        self._freeze()
        self._record_baselines()
        self._freeze_treatment()
        self._record_assisted()
        self._blind_all()
        event = self._call(
            record_value_gate_decision,
            self.config,
            decision="CONTINUE",
            notes="All frozen value gates passed under independent review.",
            evidence_ref="archive:value-pack",
            actor="human:supervisor",
            actor_role="research_supervisor",
        )
        self.assertFalse(event["payload"]["automatic_activation"])
        self.assertEqual(self._status()["operating_status"], "DECIDED_CONTINUE")

    def test_tampered_event_chain_is_invalid_and_append_fails_closed(self):
        self._open()
        path = self.root / "data/value_validation/events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"]["title"] = "tampered"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        status = self._status()
        self.assertEqual(status["integrity_status"], "FAIL")
        self.assertEqual(status["operating_status"], "INVALID")
        with self.assertRaises(ValueValidationPolicyError):
            self._call(
                register_value_case,
                self.config,
                case_id="case-1",
                case_ref="archive:case-1",
                case_sha256="1" * 64,
                priority="P1",
                stratum="filing",
                evidence_cutoff="2026-08-01T09:00:00+08:00",
                data_classification="public",
                actor="human:coord",
                actor_role="research_coordinator",
            )

    def test_status_report_is_written_without_changing_business_state(self):
        self._open()
        output = self._call(write_value_validation_status, self.config)
        self.assertTrue(Path(output["latest_json_path"]).is_file())
        self.assertTrue(Path(output["latest_path"]).is_file())
        snapshot = json.loads(Path(output["latest_json_path"]).read_text(encoding="utf-8"))
        self.assertFalse(snapshot["operation_contract"]["business_state_write"])
        self.assertFalse(snapshot["operation_contract"]["trading_or_order_action"])


if __name__ == "__main__":
    unittest.main()
