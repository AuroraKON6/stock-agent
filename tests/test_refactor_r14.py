"""R14: personal workspace closed loop — create/cancel/followup/rerun over
the real board HTTP surface, plus the SSE resume contract.

Service-level and HTTP-level (O) coverage of the taskbook's A64, A90–A92 and
the SSE cursor (A65 O half). The browser B-layer (isolated Chromium run of
the full page flow) stays with the existing browser harness acceptance —
see report for status.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.identity_providers import PersonalSessionStore, SESSION_COOKIE_NAME
from stock_agent.personal_workspace import (
    WorkspaceError,
    create_personal_research,
    rerun_completed_research,
)
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


def _replay(with_questions=True):
    return {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": "基于公开上下文的研究草稿。",
        "key_findings": [], "unknowns": [],
        "evidence_refs": [],
        "human_questions": ([{"question": "是否补充原件？", "why_needed": "确认",
                              "options": []}] if with_questions else []),
        "model_self_assessed_confidence": 0.4}


class R14BoardTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

        def authenticator(config, actor, symbols):
            if actor != "lead-analyst":
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-fixture", "policy_sha256": "f" * 64}

        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=PersonalSessionStore(ttl_seconds=900),
            login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.cookie = self._login()

    # ---------------- HTTP helpers ---------------- #

    def _request(self, method, path, body=None, *, cookie=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"}
            if cookie is None:
                cookie = self.cookie  # default: the logged-in session
            if cookie != "":
                headers["Cookie"] = cookie
            if csrf:
                headers["X-Task-Board-CSRF"] = str(self.server.csrf_token)
            payload = json.dumps(body) if body is not None else None
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            data = response.read().decode("utf-8")
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = {"raw": data}
            return response.status, parsed
        finally:
            conn.close()

    def _login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/v1/personal/login",
                         json.dumps({"actor": "lead-analyst", "symbols": ["600519"]}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            return (response.getheader("Set-Cookie") or "").split(";")[0]
        finally:
            conn.close()

    def _workspace(self, action, body):
        return self._request("POST", "/api/v1/workspace/{0}".format(action), body)

    # ---------------- task helpers ---------------- #

    def _seed_task(self, *, with_questions=True, state="awaiting"):
        spec = build_task_spec(research_question="R14 问题 {0}".format(
            self._testMethodName.__hash__() & 0xffff), symbols=["600519"],
            execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = {}
        spec["replay_response"] = _replay(with_questions)
        task, _ = self.store.create_task(title="R14", input_payload=spec,
                                         execution_mode="replay")
        if state in ("awaiting", "succeeded"):
            claimed = self.store.claim_next("w-r14")
            self.assertEqual(claimed["task_id"], task["task_id"])
            from stock_agent.research_worker import run_research_task

            run_research_task(self.store, task["task_id"], worker_id="w-r14",
                              config=self.config)
        return self.store.get_task(task["task_id"])


class WorkspaceRouteTests(R14BoardTestCase):
    def test_create_dedupes_and_returns_task(self):
        status, first = self._workspace("create", {
            "question": "白酒板块风险？", "symbols": ["600519"]})
        self.assertEqual(status, 200)
        self.assertEqual(first["outcome"], "RECORDED")
        status, second = self._workspace("create", {
            "question": "白酒板块风险？", "symbols": ["600519"]})
        self.assertEqual(second["outcome"], "DUPLICATE")
        self.assertEqual(first["task_id"], second["task_id"])

    def test_create_requires_session_and_rejects_form_actor(self):
        status, body = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "q", "symbols": ["600519"]},
            cookie="")  # empty string = deliberately NO session cookie
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "session_required")
        # With a VALID session, a form actor field is rejected (A46 rule).
        status, body = self._workspace("create", {
            "question": "q", "symbols": ["600519"], "actor": "forged"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "actor_from_session_only")

    def test_cancel_entry_lands_immediately_on_queued(self):
        self._workspace("create", {"question": "待取消？", "symbols": ["600519"]})
        task = self.store.list_tasks()[-1]
        status, body = self._workspace("cancel", {
            "task_id": task["task_id"], "expected_version": task["version"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "cancelled")

    def test_a64_followup_bumps_input_version_and_preserves(self):
        task = self._seed_task(with_questions=True)
        self.assertEqual(task["status"], "awaiting_human")
        status, body = self._workspace("followup", {
            "task_id": task["task_id"],
            "expected_version": task["version"],
            "extra_note": "补充：关注四季度"})
        self.assertEqual(status, 200)
        updated = body["task"]
        self.assertEqual(updated["input_version"], task["input_version"] + 1)
        self.assertEqual(updated["status"], "queued")
        # Previous input preserved in the snapshot chain (R01).
        snapshots = self.store.input_snapshots(task["task_id"])
        self.assertEqual([s["input_version"] for s in snapshots],
                         [task["input_version"], task["input_version"] + 1])

    def test_followup_rejected_when_not_awaiting(self):
        task = self._seed_task(with_questions=False, state="succeeded")
        status, body = self._workspace("followup", {
            "task_id": task["task_id"], "expected_version": task["version"],
            "note": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "not_awaiting")


class A90A91A92Tests(R14BoardTestCase):
    def _finished_task(self, *, with_questions=False):
        task = self._seed_task(with_questions=with_questions, state="succeeded")
        self.assertEqual(task["status"], "succeeded")
        return task

    def _finished_via_workspace(self, label: str):
        """The product path: create via the workspace route (question key),
        run to awaiting, acknowledge to a personal-terminal completion."""
        status, created = self._workspace("create", {
            "question": "R14 完成研究 {0}".format(label), "symbols": ["600519"]})
        self.assertEqual(status, 200)
        task_id = created["task_id"]
        claimed = self.store.claim_next("w-r14")
        self.assertEqual(claimed["task_id"], task_id)
        from stock_agent.research_worker import run_research_task

        run_research_task(self.store, task_id, worker_id="w-r14", config=self.config)
        awaiting = self.store.get_task(task_id)
        self.assertEqual(awaiting["status"], "awaiting_human")
        done = self.store.record_human_response(
            task_id, actor="lead-analyst", decision="acknowledge",
            expected_version=awaiting["version"], command_id="cmd-{0}".format(label))
        self.assertEqual(done["status"], "succeeded")
        return done

    def test_a90_no_model_question_still_allows_rerun(self):
        task = self._finished_via_workspace("a90")
        status, body = self._workspace("rerun", {"task_id": task["task_id"]})
        self.assertEqual(status, 200)
        self.assertNotEqual(body["task_id"], task["task_id"])
        # The OLD result is untouched; the new task carries the prior link.
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "succeeded")
        origin = self.store.get_task(body["task_id"])["input_payload"]["origin"]
        self.assertEqual(origin["prior_task_id"], task["task_id"])
        self.assertEqual(origin["research_occasion"], "explicit_new_research")

    def test_a91_acknowledged_then_new_research_old_result_untouched(self):
        task = self._finished_via_workspace("a91")
        old_output = dict(task["output"])
        status, body = self._workspace("rerun", {"task_id": task["task_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get_task(task["task_id"])["output"], old_output,
                         "the acknowledged result is never modified by a rerun")

    def test_a92_same_question_new_day_distinct_retry_dedupes(self):
        task = self._finished_via_workspace("a92")
        first = rerun_completed_research(self.store, actor="lead-analyst",
                                         task_id=task["task_id"], config=self.config)
        second = rerun_completed_research(self.store, actor="lead-analyst",
                                          task_id=task["task_id"], config=self.config)
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(second["status"], "RECORDED")
        self.assertNotEqual(first["task"]["task_id"], second["task"]["task_id"],
                            "two explicit reruns are two new intents")
        # An intent-scoped retry of ONE submit still deduplicates (R01).
        retried = create_personal_research(
            self.store, actor="lead-analyst", question="R14 重试问题？",
            symbols=["600519"], config=self.config, intent_id="qi-r14-fixed")
        again = create_personal_research(
            self.store, actor="lead-analyst", question="R14 重试问题？",
            symbols=["600519"], config=self.config, intent_id="qi-r14-fixed")
        self.assertEqual(retried["status"], "RECORDED")
        self.assertEqual(again["status"], "DUPLICATE")

    def test_rerun_rejected_while_running(self):
        spec = build_task_spec(research_question="运行中任务？", symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = {}
        spec["replay_response"] = _replay(True)
        task, _ = self.store.create_task(title="R14-run", input_payload=spec,
                                         execution_mode="replay")
        with self.assertRaises(WorkspaceError) as caught:
            rerun_completed_research(self.store, actor="lead-analyst",
                                     task_id=task["task_id"], config=self.config)
        self.assertIn("task_terminal", str(caught.exception))


class SSEResumeTests(R14BoardTestCase):
    """A65 O half: the outbox cursor replays strictly after Last-Event-ID —
    no lost terminal event, no duplicate."""

    def test_events_after_cursor_no_loss_no_dup(self):
        task = self._seed_task(with_questions=False, state="succeeded")
        events = self.store.events(task["task_id"], limit=1000)
        self.assertTrue(events)
        # Reconnect from the middle: everything AFTER the cursor arrives
        # exactly once, in order.
        cursor = events[len(events) // 2]["sequence"]
        resumed = self.store.events_after(cursor, limit=1000)
        expected = [e["sequence"] for e in events if e["sequence"] > cursor]
        self.assertEqual([e["sequence"] for e in resumed], expected)
        # And the terminal event is never missed by a tail resume.
        last = events[-1]["sequence"]
        self.assertEqual(self.store.events_after(last - 1, limit=10)[0]["sequence"], last)


if __name__ == "__main__":
    unittest.main()
