"""R17: the controlled role entry Dify calls — offline, over real HTTP forms.

These tests sit where the Dify canvas sits: native HTTP-node forms posted to
the loopback listener. They drive the frozen flow artifacts loaded from disk,
the gated entry, the R22 subtask graph and the standard worker boundary in
replay mode, and assert A76–A79's Python-side behaviour:

- A76  binding closes the task to the full chain; order / ownership / role /
       input-version / case / scope / digest mismatches are refused;
- A77  approvals cover one digest: a prompt or canvas edit is refused;
- A78  one step task per role, attempts bounded by the step budget, replays
       and repeats re-execute nothing, the ledger reconciles per step;
- A79  a replayed run lands on the bound task and continues it; closing the
       role entry (rollback) refuses new triggers and executes nothing.
"""

import hashlib
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

from stock_agent.config import project_path_override
from stock_agent.dify_api import DifyGateway
from stock_agent.dify_api_server import create_dify_api_server
from stock_agent.dify_deployment import DeploymentCandidateError, validate_role_workflow
from stock_agent.role_execution import ArtifactFlowRegistry, RoleFlowError

FLOW_DIR = Path(__file__).resolve().parents[1] / "config/candidates/dify/flows"
TOKEN = "role-test-token"
LIMITED = "create-only-token"
ROLES = ("event_analyst", "counter_evidence", "synthesis_drafter")
WORKFLOW = "sha256:" + "a" * 64


def answer(summary):
    return {"schema_version": "1.0", "task_id": "placeholder", "summary": summary, "key_findings": [],
            "unknowns": [], "evidence_refs": [], "human_questions": [], "model_self_assessed_confidence": 0.5}


class RoleEntryTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Ledgers resolve under the project root: keep them out of the checkout.
        override = project_path_override(lambda value: self.root / value)
        override.__enter__()
        self.addCleanup(override.__exit__, None, None, None)
        self.flows = self.root / "flows"
        shutil.copytree(FLOW_DIR, self.flows)
        self.registry = ArtifactFlowRegistry.from_directory(self.flows)
        manifest = json.loads((self.flows / "event-role-flow.manifest.json").read_bytes())
        self.flow_sha256 = manifest["flow_sha256"]
        self.replay = self.root / "replay"
        credentials_path = self.root / "config/dify/service_credentials.json"
        credentials_path.parent.mkdir(parents=True)
        credentials_path.write_text(json.dumps({"schema_version": "1.0", "credentials": [
            {"credential_id": "orchestrator", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
             "scopes": ["create_research", "run_role_flow"], "case_scope": ["case-r17"], "active": True,
             "created_at": "2026-11-24T00:00:00+00:00"},
            {"credential_id": "create-only", "token_sha256": hashlib.sha256(LIMITED.encode()).hexdigest(),
             "scopes": ["create_research"], "case_scope": ["case-r17"], "active": True,
             "created_at": "2026-11-24T00:00:00+00:00"},
        ]}))
        self.config = {
            "dify": {"enabled": True, "service_credentials_path": "config/dify/service_credentials.json",
                     "approved_workflow_versions": [WORKFLOW]},
            "task_runtime": {"db_path": str(self.root / "tasks.sqlite3")},
        }
        self.server = None
        self.addCleanup(self.close_entry)
        self.open_entry()

    def open_entry(self, **role_settings):
        """Serve the role entry with the given role settings (defaults: enabled)."""
        if self.server is not None:
            self.close_entry()
        from stock_agent.task_runtime import TaskStore

        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        settings = dict(role_flows=self.registry, role_replay_dir=self.replay,
                        role_workflow_version=WORKFLOW)
        self.gateway = DifyGateway(self.store, self.config, **{**settings, **role_settings})
        self.server = create_dify_api_server(self.gateway, port=0, replay_forms=True)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

    def close_entry(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.store.close()

    # ---------------- helpers ---------------- #

    def post(self, operation, fields, token=TOKEN):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("POST", "/api/dify/v1/" + operation, urlencode(fields), {
                "Content-Type": "application/x-www-form-urlencoded", "Authorization": "Bearer " + token})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, json.load(response)
        finally:
            connection.close()

    def create(self, run_id, case_id="case-r17", question="合成事件的影响？", intent="intent-r17"):
        code, headers, reply = self.post("create-research", {
            "title": "frozen event", "case_id": case_id, "research_question": question,
            "input_version": "1", "business_intent_id": intent, "dify_run_id": run_id,
            "workflow_version": WORKFLOW, "execution_mode": "replay"})
        self.assertEqual(code, 200, reply)
        return reply["data"]["task_id"]

    def form(self, task_id, run_id="run-1", **extra):
        return {"task_id": task_id, "case_id": "case-r17", "dify_run_id": run_id,
                "workflow_version": WORKFLOW, "flow_sha256": self.flow_sha256, **extra}

    def bind(self, task_id, run_id="run-1", version="1"):
        code, headers, reply = self.post("role-bind", self.form(task_id, run_id, input_version=version))
        self.assertEqual(code, 200, reply)
        return reply["data"]

    def fixture(self, role, document, name=None):
        folder = self.replay / "case-r17"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name or role + ".json")).write_text(
            json.dumps(document, ensure_ascii=False) if document is not None else "null")

    def drive(self, task_id, run_id="run-1", roles=ROLES, version="2"):
        """The canvas in order: every role step, then finish. Returns (code, reply) per call."""
        return [self.post(operation, self.form(task_id, run_id, input_version=version, **extra))
                for operation, extra in [("role-step", {"role_id": role}) for role in roles] + [("role-finish", {})]]

    def steps(self, task_id):
        return sorted((task for task in self.store.list_tasks()
                       if task.get("parent_task_id") == task_id), key=lambda task: task["task_id"])

    def ledger(self):
        path = self.root / "data/dify/role_flow.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class HappyPathTests(RoleEntryTestCase):
    def test_canvas_forms_run_the_whole_flow_once(self):
        for role in ROLES:
            self.fixture(role, answer(role + " 完成"))
        task_id = self.create("run-1")
        bound = self.bind(task_id)
        self.assertEqual((bound["bind_result"], bound["input_version"]), ("bound", 2))
        replies = self.drive(task_id)
        self.assertEqual([code for code, _, _ in replies], [200] * 4, [body for _, _, body in replies])
        for (_, headers, reply), role in zip(replies[:3], ROLES):
            data = reply["data"]
            self.assertEqual((data["step_status"], data["executed"]), ("succeeded", True))
            self.assertEqual(headers["r17_step_status"], "succeeded")
            self.assertEqual(data["metering"]["attempts"], 1)
            self.assertEqual(data["metering"]["mode"], "replay")
            self.assertFalse(data["metering"]["model_called"])
        finished = replies[3][2]["data"]
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(sorted(finished["role_outputs"]), sorted(ROLES))
        self.assertEqual([item["attempts"] for item in finished["steps"]], [1, 1, 1])
        root = self.store.get_task(task_id)
        self.assertEqual(root["status"], "succeeded")
        self.assertFalse(root["model_call"]["called"], "the root makes no model call of its own")
        self.assertEqual(len(root["model_call"]["steps"]), 3)
        self.assertEqual(len(self.steps(task_id)), 3, "exactly one step task per role")
        # A77: what actually ran is ledgered under the approved digest.
        spent = [entry for entry in self.ledger() if entry["executed"]]
        self.assertEqual({entry["flow_sha256"] for entry in spent}, {self.flow_sha256})
        self.assertEqual({entry["approved_by"] for entry in spent}, {"r17-candidate-review"})

    def test_replayed_run_lands_on_the_bound_task_and_reexecutes_nothing(self):
        for role in ROLES:
            self.fixture(role, answer(role + " 完成"))
        task_id = self.create("run-1")
        self.bind(task_id)
        self.drive(task_id)
        before = [(task["task_id"], task["attempt_count"]) for task in self.store.list_tasks()]
        # Same business intent, NEW Dify run: no second task, no second billing.
        self.assertEqual(self.create("run-2"), task_id)
        # The canvas reads the version off the create receipt (the task is at 2).
        code, headers, _ = self.post("research-status", {"task_id": task_id, "status_only": "true"})
        self.assertEqual(self.bind(task_id, run_id="run-2", version="2")["bind_result"], "already_bound")
        self.refused_bind_stale = None
        replies = self.drive(task_id, run_id="run-2")
        self.assertEqual([code for code, _, _ in replies], [200] * 4, [body for _, _, body in replies])
        self.assertTrue(all(reply["data"]["executed"] is False for _, _, reply in replies))
        self.assertTrue(replies[-1][2]["data"]["already_complete"])
        self.assertEqual([(task["task_id"], task["attempt_count"]) for task in self.store.list_tasks()], before)
        self.assertEqual(len(self.store.list_platform_runs(task_id)), 2, "both runs are associated")

    def test_a_replayed_create_with_different_content_is_still_a_conflict(self):
        self.fixture(ROLES[0], answer("分析"))
        task_id = self.create("run-1")
        self.bind(task_id)
        code, _, reply = self.post("create-research", {
            "title": "frozen event", "case_id": "case-r17", "research_question": "另一个问题",
            "input_version": "1", "business_intent_id": "intent-r17", "dify_run_id": "run-2",
            "workflow_version": WORKFLOW, "execution_mode": "replay"})
        self.assertEqual(code, 409, reply)
        self.assertEqual(len(self.store.list_tasks()), 1)


class RefusalTests(RoleEntryTestCase):
    """A76: the canvas may not skip, repeat, reorder or redirect — the entry decides."""

    def setUp(self):
        super().setUp()
        self.task_id = self.create("run-1")
        self.bind(self.task_id)

    def refused(self, operation, fields, status, reason, token=TOKEN):
        code, headers, reply = self.post(operation, fields, token)
        self.assertEqual((code, reply.get("error")), (status, reason), reply)
        self.assertNotIn("r17_step_status", headers, "a refusal hands the canvas no receipt")
        return reply

    def test_out_of_order_role_is_refused_and_creates_nothing(self):
        self.refused("role-step", self.form(self.task_id, role_id="synthesis_drafter", input_version="2"),
                     409, "group_not_current")
        self.assertEqual(self.steps(self.task_id), [])

    def test_role_outside_the_frozen_flow_is_refused(self):
        self.refused("role-step", self.form(self.task_id, role_id="quality_check", input_version="2"),
                     409, "role_not_in_flow")

    def test_case_run_version_digest_and_workflow_mismatches_are_refused(self):
        base = self.form(self.task_id, role_id="event_analyst", input_version="2")
        hidden = (403, "task_not_found_or_out_of_scope")
        self.refused("role-step", {**base, "case_id": "case-other"}, *hidden)
        self.refused("role-step", {**base, "dify_run_id": "run-never-recorded"}, *hidden)
        self.refused("role-step", {**base, "input_version": "1"}, 409, "input_version_mismatch")
        self.refused("role-step", {**base, "flow_sha256": "e" * 64}, 409, "flow_not_registered")
        self.refused("role-step", {**base, "workflow_version": "sha256:" + "0" * 64},
                     403, "workflow_version_not_approved")
        self.refused("role-finish", {key: value for key, value in base.items() if key != "role_id"},
                     409, "flow_incomplete")
        self.assertEqual(self.steps(self.task_id), [], "no refusal executed a step")

    def test_credential_scope_and_case_scope_are_enforced(self):
        base = self.form(self.task_id, role_id="event_analyst", input_version="2")
        self.refused("role-step", base, 403, "scope_not_granted", token=LIMITED)
        self.refused("role-bind", self.form(self.task_id, input_version="1"), 403, "scope_not_granted",
                     token=LIMITED)
        registry = json.loads((self.root / "config/dify/service_credentials.json").read_text())
        registry["credentials"][0]["case_scope"] = ["case-other"]
        (self.root / "config/dify/service_credentials.json").write_text(json.dumps(registry))
        self.refused("role-step", base, 403, "task_not_found_or_out_of_scope")

    def test_unbound_and_terminal_tasks_are_refused(self):
        fresh = self.create("run-9", intent="intent-unbound")
        code, _, reply = self.post("role-step", self.form(fresh, run_id="run-9", role_id="event_analyst",
                                                         input_version="1"))
        self.assertEqual((code, reply["error"]), (409, "task_not_bound"), reply)
        for role in ROLES:
            self.fixture(role, answer(role + " 完成"))
        self.drive(self.task_id, roles=ROLES[:1])
        finished = self.drive(self.task_id, roles=ROLES[1:])
        self.assertEqual([code for code, _, _ in finished], [200, 200, 200], [b for _, _, b in finished])
        # A replayed run against the completed root: the step is handed back, not re-run.
        again = self.drive(self.task_id)
        self.assertEqual([code for code, _, _ in again], [200] * 4, [b for _, _, b in again])
        self.assertTrue(all(reply["data"]["executed"] is False for _, _, reply in again))

    def test_form_shape_is_part_of_the_contract(self):
        base = self.form(self.task_id, role_id="event_analyst", input_version="2")
        for fields in ({**base, "extra": "1"}, {key: value for key, value in base.items() if key != "role_id"},
                       {**base, "input_version": "0"}, {**base, "input_version": "2.0"}):
            code, _, _ = self.post("role-step", fields)
            self.assertEqual(code, 400, fields)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        body = urlencode([("task_id", self.task_id), ("task_id", self.task_id)])
        connection.request("POST", "/api/dify/v1/role-step", body,
                           {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(connection.getresponse().status, 400, "duplicate form fields")
        connection.close()


class BoundedCycleTests(RoleEntryTestCase):
    """A78: steps, retries and budget are finite, and everything spent is on the ledger."""

    def test_concurrent_callbacks_share_one_step_and_attempt(self):
        self.fixture(ROLES[0], answer("分析"))
        task_id = self.create("run-1")
        self.bind(task_id)
        fields = self.form(task_id, input_version="2", role_id=ROLES[0])
        start = threading.Barrier(4)

        def callback(_index):
            start.wait(timeout=5)
            return self.post("role-step", fields)

        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(callback, range(4)))
        self.assertTrue(all(code in {200, 409} for code, _, _ in replies))
        self.assertEqual(sum(bool(reply.get("data", {}).get("executed")) for _, _, reply in replies), 1)
        step, = self.steps(task_id)
        self.assertEqual((step["status"], step["attempt_count"]), ("succeeded", 1))

    def test_role_output_cannot_cite_evidence_outside_the_approved_context(self):
        bad_answer = answer("事件影响待确认")
        bad_answer["evidence_refs"] = ["synthetic-unapproved-reference"]
        self.fixture(ROLES[0], bad_answer)
        task_id = self.create("run-1")
        self.bind(task_id)
        code, _, reply = self.post("role-step", self.form(task_id, input_version="2", role_id=ROLES[0]))
        self.assertEqual((code, reply["data"]["step_status"]), (200, "queued"))
        step, = self.steps(task_id)
        self.assertIn("current approved context", step["last_error"])
        self.assertFalse(step["output"])

    def test_a_retry_is_the_same_step_task_and_stops_at_its_budget(self):
        self.fixture(ROLES[0], answer("分析"))
        self.fixture(ROLES[2], answer("综合"))
        task_id = self.create("run-1")
        self.bind(task_id)
        self.post("role-step", self.form(task_id, input_version="2", role_id="event_analyst"))
        base = self.form(task_id, input_version="2", role_id="counter_evidence")
        counters = lambda: [task for task in self.steps(task_id)  # noqa: E731
                            if task["role_id"] == "counter_evidence"]
        # No recorded answer for attempt 1: one bounded attempt, requeued, no twin.
        code, _, reply = self.post("role-step", base)
        self.assertEqual((code, reply["data"]["step_status"], reply["data"]["executed"]), (200, "queued", True))
        counter = counters()[0]
        self.assertEqual((counter["status"], counter["attempt_count"], counter["max_attempts"]), ("queued", 1, 2))
        # The answer for the second attempt exists now: the SAME task runs it.
        self.fixture(ROLES[1], answer("反证"), name="counter_evidence.attempt-2.json")
        code, _, reply = self.post("role-step", base)
        self.assertEqual((code, reply["data"]["step_status"], reply["data"]["step_task_id"]),
                         (200, "succeeded", counter["task_id"]))
        self.assertEqual(self.store.get_task(counter["task_id"])["attempt_count"], 2)
        self.assertEqual(len(counters()), 1, "one step task for the role")
        code, _, reply = self.post("role-step", self.form(task_id, input_version="2", role_id=ROLES[2]))
        self.assertEqual((code, reply["data"]["step_status"]), (200, "succeeded"), reply)
        code, _, reply = self.post("role-finish", self.form(task_id, input_version="2"))
        self.assertEqual((code, reply["data"]["status"]), (200, "succeeded"))

    def test_exhausted_attempts_fail_the_step_and_never_auto_retry(self):
        for name in ("counter_evidence.attempt-1", "counter_evidence.attempt-2"):
            self.fixture(ROLES[1], None, name=name)
        self.fixture(ROLES[0], answer("分析"))
        task_id = self.create("run-1")
        self.bind(task_id)
        base = self.form(task_id, input_version="2", role_id="counter_evidence")
        self.fixture(ROLES[0], answer("分析"))
        self.assertEqual(self.post("role-step", self.form(task_id, input_version="2", role_id=ROLES[0]))[0], 200)
        outcomes = [self.post("role-step", base)[2]["data"]["step_status"] for _ in range(2)]
        self.assertEqual(outcomes, ["queued", "failed"])
        counter = [task for task in self.steps(task_id) if task["role_id"] == "counter_evidence"][0]
        self.assertEqual((counter["status"], counter["attempt_count"]), ("failed", 2))
        code, _, reply = self.post("role-step", base)
        self.assertEqual((code, reply["error"]), (409, "step_failed_needs_decision"))
        self.assertEqual(self.store.get_task(counter["task_id"])["attempt_count"], 2, "nothing more was spent")
        self.assertEqual(len([task for task in self.steps(task_id)
                              if task["role_id"] == "counter_evidence"]), 1)

    def test_quality_gate_blocks_a_role_step_like_the_full_chain(self):
        self.fixture(ROLES[0], answer("分析"))
        self.fixture(ROLES[1], answer("公告称回购金额为 3 亿元。"))
        task_id = self.create("run-1")
        self.bind(task_id)
        statuses = [self.post("role-step", self.form(task_id, input_version="2", role_id=role))[2]["data"]
                    ["step_status"] for role in ROLES[:2]]
        self.assertEqual(statuses, ["succeeded", "blocked"])
        counter = [task for task in self.steps(task_id) if task["role_id"] == "counter_evidence"][0]
        self.assertEqual(counter["model_call"]["governance"]["gate"], "evidence_quality")
        code, _, reply = self.post("role-step", self.form(task_id, input_version="2", role_id="synthesis_drafter"))
        self.assertEqual(code, 409, "a blocked step is terminal: the flow needs a decision")
        self.assertEqual(len(self.steps(task_id)), 2)

    def test_ledger_reconciles_with_the_step_tasks(self):
        for role in ROLES:
            self.fixture(role, answer(role + " 完成"))
        retry_root = self.create("run-1")
        self.bind(retry_root)
        self.post("role-step", self.form(retry_root, input_version="2", role_id="counter_evidence"))
        self.drive(retry_root, roles=ROLES)
        spent = [entry for entry in self.ledger() if entry["operation"] == "role-step" and entry["executed"]]
        attempts = {task["task_id"]: task["attempt_count"] for task in self.store.list_tasks()
                    if task.get("parent_task_id")}
        counted = {}
        for entry in spent:
            counted[entry["step_task_id"]] = counted.get(entry["step_task_id"], 0) + 1
        self.assertEqual(counted, attempts, "every executed step call is one attempt on one step task")
        denied = [entry for entry in self.ledger() if not entry["allowed"]]
        self.assertTrue(all(entry["executed"] is False for entry in denied))


class ArtifactAndRollbackTests(RoleEntryTestCase):
    def reload(self):
        self.registry = ArtifactFlowRegistry.from_directory(self.flows)
        return self.registry

    def edited_prompt(self, *, keep_digest_consistent):
        path = self.flows / "event-role-flow.flow.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["flow"]["prompts"]["event_analyst"]["text"] += "（画布上顺手改了一句）"
        if keep_digest_consistent:
            from stock_agent.role_execution import define_role_flow

            rebuilt = define_role_flow(flow_id=document["flow"]["flow_id"], version=document["flow"]["version"],
                                       groups=document["flow"]["groups"], prompts=document["flow"]["prompts"])
            document["flow"]["flow_sha256"] = rebuilt["flow_sha256"]
        path.write_text(json.dumps(document, ensure_ascii=False))

    def test_a_prompt_edit_is_a_new_candidate_no_old_approval_covers(self):
        # Edited text under the OLD digest: the file contradicts itself.
        self.edited_prompt(keep_digest_consistent=False)
        with self.assertRaisesRegex(RoleFlowError, "artifact_digest_mismatch"):
            self.reload()
        # Edited text written down honestly as a new digest: still unapproved.
        self.edited_prompt(keep_digest_consistent=True)
        registry = self.reload()
        with self.assertRaises(RoleFlowError):
            registry.by_digest(self.flow_sha256)
        new_digest = json.loads((self.flows / "event-role-flow.flow.json").read_text())["flow"]["flow_sha256"]
        with self.assertRaises(RoleFlowError):
            registry.by_digest(new_digest)

    def test_canvas_edit_cannot_reuse_the_reviewed_version(self):
        manifest_path = self.flows / "event-role-flow.manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        approved = manifest["workflow_version"]
        self.assertEqual(validate_role_workflow(manifest_path, approved_version=approved), approved)
        artifact = self.flows / manifest["artifact"]
        artifact.write_bytes(artifact.read_bytes() + b"\n# candidate edit\n")
        with self.assertRaisesRegex(DeploymentCandidateError, "digest"):
            validate_role_workflow(manifest_path, approved_version=approved)
        manifest["workflow_version"] = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(DeploymentCandidateError, "digest"):
            validate_role_workflow(manifest_path, approved_version=approved)

    def test_rollback_continues_existing_role_binding_and_refuses_fresh_binding(self):
        for role in ROLES:
            self.fixture(role, answer(role + " 完成"))
        task_id = self.create("run-1")
        self.bind(task_id)
        self.drive(task_id, roles=ROLES[:1])
        self.open_entry(role_bind_enabled=False)
        self.assertEqual(self.create("run-2"), task_id)
        fresh = self.create("run-chain", intent="intent-chain")
        code, _, reply = self.post("role-bind", self.form(fresh, run_id="run-chain", input_version="1"))
        self.assertEqual((code, reply["error"]), (409, "role_entry_disabled"), reply)
        self.assertEqual(self.store.claim_next("full-chain", exclude_role_bound=True)["task_id"], fresh)
        replies = self.drive(task_id, run_id="run-2")
        self.assertEqual([code for code, _, _ in replies], [200] * 4)
        self.assertEqual([reply["data"]["executed"] for _, _, reply in replies],
                         [False, True, True, True])
        self.assertEqual({task["attempt_count"] for task in self.steps(task_id)}, {1})

    def test_closing_the_role_entry_refuses_new_triggers_and_executes_nothing(self):
        for role in ROLES:
            self.fixture(role, answer(role + " 完成"))
        task_id = self.create("run-1")
        self.bind(task_id)
        self.drive(task_id, roles=ROLES[:1])
        # A79 rollback: no role registry is configured — the entry is closed.
        self.close_entry()
        self.open_entry(role_flows=None)
        before = [(task["task_id"], task["attempt_count"]) for task in self.store.list_tasks()]
        for operation, extra in (("role-bind", {"input_version": "2"}),
                                 ("role-step", {"input_version": "2", "role_id": "counter_evidence"}),
                                 ("role-finish", {"input_version": "2"})):
            code, _, reply = self.post(operation, self.form(task_id, run_id="run-1", **extra))
            self.assertEqual((code, reply["error"]), (409, "role_entry_disabled"), reply)
        self.assertEqual([(task["task_id"], task["attempt_count"]) for task in self.store.list_tasks()], before)
        # New business tasks are untouched: the full chain still runs them.
        fresh = self.create("run-chain", intent="intent-chain")
        claimed = self.store.claim_next("full-chain", exclude_role_bound=True)
        self.assertEqual(claimed["task_id"], fresh)


if __name__ == "__main__":
    unittest.main()
