"""R03: request-scoped connections, atomic verification claim, dispatch-bound adoption.

Acceptance scenarios from GLM_REFACTOR_TASKBOOK_20261020 (A11–A17). Concurrency
tests use real threads racing on ONE gateway instance (the ThreadingHTTPServer
shape) — per taskbook layering, O-layer here means same-process threads over
separate store connections; cross-process N/C verification stays with R09+.
All data lives in a TemporaryDirectory via project_path_override; zero network.
"""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.dify_access import DifyAccessError
from stock_agent.dify_api import DifyGateway, DifySubmissionError
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import (
    STATUS_QUEUED,
    PrincipalVerificationError,
    TaskIdempotencyConflictError,
    TaskRuntimeError,
    TaskTransitionError,
)
from stock_agent import workflow_service

sys.path.insert(0, str(Path(__file__).resolve().parent))

TOKEN = "r03-dify-token"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class R03GatewayTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self._write_credentials()
        self.config = {
            "dify": {
                "enabled": True,
                "service_credentials_path": "config/dify/service_credentials.json",
                "approved_workflow_versions": ["wf-r03", "wf-r03b"],
            },
            "task_runtime": {"budget": {"daily_model_call_limit": 2}},
        }
        self.gateway = DifyGateway(self.store, self.config)

    def _write_credentials(self, *, scopes=None, case_scope=("case-r03",),
                           credential_id="cred-r03", token=TOKEN, active=True):
        scopes = scopes or ["create_research", "read_research", "read_fact_bundle",
                            "submit_result", "submit_feedback"]
        path = self.root / "config" / "dify" / "service_credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": "1.0",
            "credentials": [{
                "credential_id": credential_id,
                "token_sha256": _digest(token),
                "scopes": list(scopes),
                "case_scope": list(case_scope),
                "active": active,
                "created_at": "2026-10-20T00:00:00+08:00",
                "expires_at": "",
            }],
        }, ensure_ascii=False), encoding="utf-8")

    def _create(self, *, case_id="case-r03", run_id="run-1", question="R03 问题？",
                input_version=1, workflow_version="wf-r03", business_intent_id=""):
        return self.gateway.create_research(
            TOKEN, title="R03 研究", case_id=case_id,
            input_version=input_version, research_question=question,
            public_context="ctx", dify_run_id=run_id,
            workflow_version=workflow_version, business_intent_id=business_intent_id)

    def _issue_bundle(self, *, case_id="case-r03", input_version=1):
        return self.gateway.issue_fact_bundle(
            case_id=case_id, input_version=input_version, facts=[
                {"fact_id": "fact-a", "kind": "market_snapshot", "source": "s",
                 "classification": "public", "external_send_allowed": True,
                 "content": "事实 A"},
            ])

    def _drive_to_awaiting_human(self, task_id, worker="w-r03"):
        task = self.store.claim_next(worker)
        self.assertEqual(task["task_id"], task_id)
        self.store.mark_running(task_id, worker_id=worker,
                                fencing_token=task["fencing_token"],
                                expected_version=task["version"])
        return self.store.request_awaiting_human(
            task_id, worker_id=worker, fencing_token=task["fencing_token"],
            human_request={"question": "补充证据？"})


class ConcurrencyTests(R03GatewayTestCase):
    def test_a11_concurrent_creates_one_task_no_dirty_state(self):
        barrier = threading.Barrier(6)
        results = []
        errors = []

        def worker(index):
            try:
                barrier.wait()
                results.append(self._create(run_id="run-{0}".format(index)))
            except Exception as exc:  # noqa: BLE001 — the test must surface all
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        outcomes = sorted(item["outcome"] for item in results)
        self.assertEqual(outcomes.count("RECORDED"), 1)
        self.assertEqual(outcomes.count("DUPLICATE"), 5)
        task_ids = {item["task_id"] for item in results}
        self.assertEqual(len(task_ids), 1,
                         "one business intent across concurrent runs = one task")
        self.assertEqual(len(self.store.list_tasks()), 1)
        # Per-request connections left the shared connection untouched and the
        # row internally consistent.
        with workflow_service.request_store(self.store.db_path) as fresh:
            task = fresh.get_task(next(iter(task_ids)))
            self.assertEqual(task["status"], STATUS_QUEUED)
            self.assertTrue(task["business_intent_id"])

    def test_a11_concurrent_mixed_writes_stay_consistent(self):
        first = self._create(run_id="w-1", question="任务一？")
        second = self._create(run_id="w-2", question="任务二？")
        self._drive_to_awaiting_human(first["task_id"])
        barrier = threading.Barrier(2)
        errors = []

        def do_cancel():
            try:
                barrier.wait()
                verification = self.gateway.issue_principal_verification(actor="lead", action="cancel")
                self.gateway.request_cancel(
                    TOKEN, task_id=second["task_id"], expected_version=1,
                    reason="并发取消", actor="lead", verification_id=verification)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def do_feedback():
            try:
                barrier.wait()
                verification = self.gateway.issue_principal_verification(actor="lead", action="feedback")
                self.gateway.submit_feedback(
                    TOKEN, task_id=first["task_id"], command_id="cmd-mix",
                    expected_version=self.store.get_task(first["task_id"])["version"],
                    decision="acknowledge", actor="lead", verification_id=verification)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_feedback)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get_task(second["task_id"])["status"], "cancelled")
        self.assertEqual(self.store.get_task(first["task_id"])["status"], "succeeded")

    def test_a12_one_verification_two_concurrent_commands_single_consumption(self):
        first = self._create(run_id="c-1", question="取消目标一？")
        second = self._create(run_id="c-2", question="取消目标二？")
        verification = self.gateway.issue_principal_verification(actor="lead-analyst")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def cancel(task_id):
            try:
                barrier.wait()
                results.append(self.gateway.request_cancel(
                    TOKEN, task_id=task_id, expected_version=1,
                    reason="并发核销", actor="lead-analyst", verification_id=verification))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=cancel, args=(first["task_id"],)),
                   threading.Thread(target=cancel, args=(second["task_id"],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        executed = [item for item in results if item.get("executed")]
        parked = [item for item in results if not item.get("executed")]
        self.assertEqual(len(executed), 1, "exactly one concurrent command may consume the receipt")
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["reason"], "principal_verification_already_used")
        statuses = {self.store.get_task(first["task_id"])["status"],
                    self.store.get_task(second["task_id"])["status"]}
        self.assertEqual(statuses, {"cancelled", STATUS_QUEUED})


class VerificationClaimTests(R03GatewayTestCase):
    def test_register_and_claim_are_atomic_and_idempotent(self):
        self.store.register_principal_verification(
            "pv-x1", actor="ops", task_id="t1", action="cancel",
            expires_at="2999-01-01T00:00:00+08:00")
        # Same issuance replay is idempotent; a different scope conflicts.
        self.store.register_principal_verification(
            "pv-x1", actor="ops", task_id="t1", action="cancel",
            expires_at="2999-01-01T00:00:00+08:00")
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.register_principal_verification(
                "pv-x1", actor="ops", task_id="t2", action="cancel",
                expires_at="2999-01-01T00:00:00+08:00")
        first = self.store.claim_principal_verification(
            "pv-x1", actor="ops", command_id="cmd-1", task_id="t1", action="cancel")
        self.assertTrue(first["claimed"])
        second = self.store.claim_principal_verification(
            "pv-x1", actor="ops", command_id="cmd-2", task_id="t1", action="cancel")
        self.assertFalse(second["claimed"])
        self.assertEqual(second["reason"], "principal_verification_already_used")
        replay = self.store.claim_principal_verification(
            "pv-x1", actor="ops", command_id="cmd-1", task_id="t1", action="cancel")
        self.assertTrue(replay["claimed"])

    def test_claim_guards_actor_task_action_and_expiry(self):
        self.store.register_principal_verification(
            "pv-x2", actor="ops", task_id="t9", action="cancel",
            expires_at="2999-01-01T00:00:00+08:00")
        cases = [
            {"actor": "someone-else", "command_id": "c", "task_id": "t9", "action": "cancel"},
            {"actor": "ops", "command_id": "c", "task_id": "other", "action": "cancel"},
            {"actor": "ops", "command_id": "c", "task_id": "t9", "action": "feedback"},
        ]
        reasons = set()
        for kwargs in cases:
            verdict = self.store.claim_principal_verification("pv-x2", **kwargs)
            self.assertFalse(verdict["claimed"])
            reasons.add(verdict["reason"])
        self.assertEqual(reasons, {"principal_actor_mismatch",
                                   "principal_task_mismatch",
                                   "principal_action_mismatch"})
        self.store.register_principal_verification(
            "pv-x3", actor="ops", expires_at="2000-01-01T00:00:00+08:00")
        expired = self.store.claim_principal_verification("pv-x3", actor="ops", command_id="c")
        self.assertEqual(expired["reason"], "principal_verification_expired")

    def test_legacy_jsonl_only_verification_migrates_on_first_claim(self):
        # A pre-R03 receipt exists ONLY in the JSONL ledger (no store row).
        from datetime import datetime, timedelta

        moment = datetime.now().astimezone()
        legacy = {
            "actor": "legacy-lead", "role": "", "task_id": "", "action": "",
            "expires_at": (moment + timedelta(minutes=10)).isoformat(),
            "issued_at": moment.isoformat(),
        }
        self.gateway._append_ledger("data/dify/principal_verifications.jsonl", {
            "event": "issued", "verification_id": "pv-legacy-1", **legacy})
        verdict = self.store.claim_principal_verification(
            "pv-legacy-1", actor="legacy-lead", command_id="cmd-legacy",
            issued_fallback=legacy)
        self.assertTrue(verdict["claimed"])
        loser = self.store.claim_principal_verification(
            "pv-legacy-1", actor="legacy-lead", command_id="cmd-other",
            issued_fallback=legacy)
        self.assertEqual(loser["reason"], "principal_verification_already_used")


class DispatchAndAdoptionTests(R03GatewayTestCase):
    def _dispatch(self, task_id, *, input_version=1, bundle_id, workflow_version="wf-r03"):
        return workflow_service.issue_workflow_dispatch(
            self.store.db_path, task_id, input_version=input_version,
            bundle_id=bundle_id, bundle_hash="hash-{0}".format(bundle_id),
            workflow_version=workflow_version)

    def _submit(self, task_id, bundle_id, *, dispatch_id="", run_id="run-1",
                workflow_version="wf-r03"):
        return self.gateway.submit_candidate_result(
            TOKEN, task_id=task_id, dify_run_id=run_id,
            workflow_version=workflow_version, bundle_id=bundle_id,
            citations=["fact-a"], result={"summary": "候选结论"},
            attempt_receipt={"calls_today": 1}, dispatch_id=dispatch_id)

    def test_happy_path_dispatch_bound_submit_then_single_adoption(self):
        created = self._create()
        bundle = self._issue_bundle()
        dispatch = self._dispatch(created["task_id"], bundle_id=bundle["bundle_id"])
        receipt = self._submit(created["task_id"], bundle["bundle_id"],
                               dispatch_id=dispatch["dispatch_id"])
        self.assertEqual(receipt["attempt_binding"], "backend_dispatch_validated")
        self.assertEqual(receipt["dispatch_id"], dispatch["dispatch_id"])
        adoption = self.gateway.adopt_candidate_result(
            TOKEN, task_id=created["task_id"], dispatch_id=dispatch["dispatch_id"],
            result_id=receipt["result_id"])
        self.assertTrue(adoption["executed"])
        self.assertEqual(adoption["status"], "adopted")
        again = self.gateway.adopt_candidate_result(
            TOKEN, task_id=created["task_id"], dispatch_id=dispatch["dispatch_id"],
            result_id=receipt["result_id"])
        self.assertFalse(again["executed"])
        self.assertEqual(again["status"], "already_adopted")

    def test_a13_forged_dispatch_and_self_reported_receipts(self):
        created = self._create()
        bundle = self._issue_bundle()
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit(created["task_id"], bundle["bundle_id"],
                         dispatch_id="wd-forged")
        self.assertEqual(caught.exception.reason, "dispatch_unknown")
        # Without a dispatch the submission is RECORDED but explicitly marked
        # caller-self-reported — data, never proof — and cannot be adopted.
        receipt = self._submit(created["task_id"], bundle["bundle_id"])
        self.assertEqual(receipt["attempt_binding"], "caller_self_reported_unverified")
        with self.assertRaises(DifySubmissionError) as caught:
            self.gateway.adopt_candidate_result(
                TOKEN, task_id=created["task_id"], dispatch_id="wd-any",
                result_id=receipt["result_id"])
        self.assertEqual(caught.exception.reason, "candidate_not_dispatch_bound")
        with self.assertRaises(DifySubmissionError) as caught:
            self.gateway.adopt_candidate_result(
                TOKEN, task_id=created["task_id"], dispatch_id="wd-any",
                result_id="cr-does-not-exist")
        self.assertEqual(caught.exception.reason, "candidate_result_unknown")

    def test_a14_wrong_bundle_or_workflow_rejected(self):
        created = self._create()
        bundle = self._issue_bundle()
        dispatch = self._dispatch(created["task_id"], bundle_id=bundle["bundle_id"])
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit(created["task_id"], "fb-other-bundle",
                         dispatch_id=dispatch["dispatch_id"])
        self.assertEqual(caught.exception.reason, "dispatch_binding_mismatch")
        # Same generation, different (also approved) workflow version than the
        # one the dispatch was issued for.
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit(created["task_id"], bundle["bundle_id"],
                         dispatch_id=dispatch["dispatch_id"],
                         workflow_version="wf-r03b")
        self.assertEqual(caught.exception.reason, "dispatch_binding_mismatch")

    def test_a15_late_submission_against_old_generation_rejected(self):
        created = self._create()
        bundle = self._issue_bundle()
        dispatch = self._dispatch(created["task_id"], bundle_id=bundle["bundle_id"])
        # The task moves on: human evidence supplement bumps input_version AND
        # the row version — the dispatch was issued for a world that ended.
        awaiting = self._drive_to_awaiting_human(created["task_id"])
        self.store.record_human_response(
            created["task_id"], actor="lead-analyst", decision="provide_input",
            expected_version=awaiting["version"], command_id="cmd-bump",
            input_patch={"extra_note": "新证据"})
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit(created["task_id"], bundle["bundle_id"],
                         dispatch_id=dispatch["dispatch_id"])
        self.assertEqual(caught.exception.reason, "dispatch_generation_stale")
        with self.assertRaises(DifySubmissionError) as caught:
            self.gateway.adopt_candidate_result(
                TOKEN, task_id=created["task_id"], dispatch_id=dispatch["dispatch_id"],
                result_id="cr-any")
        self.assertEqual(caught.exception.reason, "candidate_result_unknown")

    def test_concurrent_adoption_of_one_dispatch_single_winner(self):
        created = self._create()
        bundle = self._issue_bundle()
        dispatch = self._dispatch(created["task_id"], bundle_id=bundle["bundle_id"])
        receipt = self._submit(created["task_id"], bundle["bundle_id"],
                               dispatch_id=dispatch["dispatch_id"])
        barrier = threading.Barrier(2)
        results, rejections = [], []

        def adopt():
            try:
                barrier.wait()
                results.append(self.gateway.adopt_candidate_result(
                    TOKEN, task_id=created["task_id"],
                    dispatch_id=dispatch["dispatch_id"], result_id=receipt["result_id"]))
            except DifySubmissionError as exc:
                # The race loser may legitimately surface the guarded UPDATE
                # as a 409 (dispatch_already_consumed) — that IS the
                # single-winner guarantee working.
                rejections.append(exc.reason)

        threads = [threading.Thread(target=adopt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        adopted = [item for item in results if item.get("executed")]
        self.assertEqual(len(adopted), 1)
        losers = [item for item in results if not item.get("executed")] + rejections
        self.assertEqual(len(losers), 1)
        self.assertTrue(all("already" in str(item) for item in losers),
                        "the loser must be an already-* outcome, never a second adoption")


class CredentialRevocationTests(R03GatewayTestCase):
    def test_a16_revocation_takes_effect_on_the_next_request(self):
        created = self._create()
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(view["task_id"], created["task_id"])
        # Revoke every credential on disk (the gateway holds no injected
        # registry): the NEXT request must fail authentication, not a later
        # cache flush.
        path = self.root / "config" / "dify" / "service_credentials.json"
        path.write_text(json.dumps({"schema_version": "1.0", "credentials": []},
                                   ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(DifyAccessError) as caught:
            self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(caught.exception.reason, "credential_unknown")
        # Re-issuing the credential on disk is picked up on the next request
        # too (the reload is per request, not a one-shot).
        self._write_credentials()
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(view["task_id"], created["task_id"])

    def test_injected_credentials_remain_a_fixed_test_seam(self):
        injected = {"schema_version": "1.0", "credentials": []}
        gateway = DifyGateway(self.store, self.config, credentials=injected)
        with self.assertRaises(DifyAccessError) as caught:
            gateway.research_status(TOKEN, task_id="any")
        self.assertEqual(caught.exception.reason, "credential_unknown")


class ScopeUniformityTests(R03GatewayTestCase):
    def test_a17_foreign_and_missing_tasks_share_one_reason(self):
        created = self._create(case_id="case-r03")
        self._write_credentials(case_scope=["case-other"])
        gateway = DifyGateway(self.store, self.config)
        reasons = set()
        for task_id in (created["task_id"], "task-does-not-exist"):
            with self.assertRaises(DifyAccessError) as caught:
                gateway.research_status(TOKEN, task_id=task_id)
            reasons.add(caught.exception.reason)
        self.assertEqual(reasons, {"task_not_found_or_out_of_scope"})


if __name__ == "__main__":
    unittest.main()
