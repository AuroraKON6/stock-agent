"""R20 遗留补项: workbench read/write ownership over the real board surface.

Two isolated synthetic sessions (alice, bob) share one temporary task store.
The contract under test is the same one R15's server-side binding and R20's
library scope use:

- shared research (no trusted personal binding) is readable by every signed-in
  principal — the documented compatibility path for generic/pipeline/historical
  tasks that predate the binding table;
- a personal task bound by the workspace entry is readable ONLY by its owner;
  everybody else gets the missing-task answer (no title, summary, report,
  input or document leak);
- the realtime stream carries no out-of-scope task events;
- a rerun is allowed only for the bound owner, and a denied rerun leaves the
  task store, bindings, queue and model dispatch untouched.

All data is synthetic, every run is replay, no external request is made.
"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.identity_providers import PersonalSessionStore
from stock_agent.personal_evidence import PERSONAL_ORIGIN_VIA
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

ALICE = "alice-analyst"
BOB = "bob-analyst"


def _spec(question: str, config=None) -> dict:
    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=config or {})
    spec.setdefault("public_context", {})
    spec["public_context"]["evidence_summaries"] = {}
    spec["bound_signal_ids"] = []
    return spec


class _Board:
    def __init__(self, store, config, *, sessions=True, clock=None):
        self.store = store
        self.config = config
        self.session_store = (PersonalSessionStore(ttl_seconds=900, clock=clock)
                              if sessions else None)

        def authenticator(config, actor, symbols):
            if actor not in (ALICE, BOB) or "600519" not in symbols:
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-" + actor, "policy_sha256": "f" * 64}

        self.server = create_task_board_server(
            store, host="127.0.0.1", port=0, config=config,
            session_store=self.session_store,
            login_authenticator=authenticator if sessions else None)
        # Keep any SSE test short and deterministic.
        self.server.sse_max_seconds = 6
        self.server.sse_keepalive_seconds = 0.2
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, *, cookie="", csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            if csrf:
                headers["X-Task-Board-CSRF"] = str(self.server.csrf_token)
            payload = json.dumps(body) if body is not None else None
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return response.status, parsed, (response.getheader("Set-Cookie") or "")
        finally:
            conn.close()

    def login(self, actor):
        status, body, set_cookie = self.request(
            "POST", "/api/v1/personal/login",
            {"actor": actor, "symbols": ["600519"]})
        assert status == 200, (status, body)
        return set_cookie.split(";")[0]

    def sse(self, cookie="", last_event_id=0):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": "127.0.0.1"}
        if cookie:
            headers["Cookie"] = cookie
        if last_event_id:
            headers["Last-Event-ID"] = str(last_event_id)
        conn.request("GET", "/api/v1/events", headers=headers)
        return conn, conn.getresponse()


def _read_sse_event(response) -> dict:
    event = {"id": "", "data": ""}
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("SSE closed before an event arrived")
        text = line.decode("utf-8").rstrip("\n\r")
        if text == "":
            if event["id"] or event["data"]:
                event["data"] = json.loads(event["data"] or "{}")
                return event
            continue
        if text.startswith(":"):
            continue
        if text.startswith("id:"):
            event["id"] = text[3:].strip()
        elif text.startswith("data:"):
            event["data"] = text[5:].strip()


class WorkspaceOwnershipTests(unittest.TestCase):
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
        # Mutable clock: every session decision (login/resolve) reads it, so
        # a test can expire an ALREADY OPEN stream deterministically.
        self.clock_now = [datetime.now(timezone.utc)]
        self.board = _Board(self.store, self.config,
                            clock=lambda: self.clock_now[0])
        self.addCleanup(self.board.close)
        self.alice_cookie = self.board.login(ALICE)
        self.bob_cookie = self.board.login(BOB)

    # ---------------- helpers ---------------- #

    def _create_personal(self, cookie, question):
        status, body, _ = self.board.request(
            "POST", "/api/v1/workspace/create",
            {"question": question, "symbols": ["600519"]}, cookie=cookie)
        self.assertEqual(status, 200, body)
        return body["task_id"]

    def _finish(self, task_id, *, actor):
        from stock_agent.research_worker import run_research_task

        claimed = self.store.claim_next("w-own")
        self.assertEqual(claimed["task_id"], task_id)
        run_research_task(self.store, task_id, worker_id="w-own", config=self.config)
        task = self.store.get_task(task_id)
        if task["status"] == "awaiting_human":
            task = self.store.record_human_response(
                task_id, actor=actor, decision="acknowledge",
                expected_version=task["version"], command_id="own-" + task_id)
        self.assertEqual(task["status"], "succeeded")
        return task

    def _counts(self):
        return {
            "tasks": len(self.store.list_tasks()),
            "bindings": len(self.store.input_owner_bindings()),
            "queue": len(self.store.list_tasks(status="queued")),
            "events": int(self.store.summary()["outbox_events"]),
            "attempts": len([event for event in self.store.events(limit=10000)
                             if event.get("event_type") == "research_attempt_started"]),
        }

    # ---------------- list / detail read scope ---------------- #

    def test_list_and_detail_are_scoped_to_the_session(self):
        alice_id = self._create_personal(self.alice_cookie, "ALICE 的样本研究？")
        self._finish(alice_id, actor=ALICE)
        bob_id = self._create_personal(self.bob_cookie, "BOB 的机密研究？")
        self._finish(bob_id, actor=BOB)
        shared, _ = self.store.create_task(
            title="共享任务", input_payload=_spec("共享通用研究？", self.config),
            execution_mode="replay")

        status, alice_state, _ = self.board.request(
            "GET", "/api/v1/tasks", cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 200)
        alice_ids = {task["task_id"] for task in alice_state["tasks"]}
        self.assertIn(alice_id, alice_ids)
        self.assertIn(shared["task_id"], alice_ids)
        self.assertNotIn(bob_id, alice_ids, "another owner's personal task leaked")
        self.assertNotIn("BOB 的机密研究", json.dumps(alice_state, ensure_ascii=False))
        self.assertEqual(sum(alice_state["summary"]["task_counts"].values()),
                         len(alice_ids), "summary counts must be scoped too")

        status, bob_state, _ = self.board.request(
            "GET", "/api/v1/tasks", cookie=self.bob_cookie, csrf=False)
        bob_ids = {task["task_id"] for task in bob_state["tasks"]}
        self.assertIn(bob_id, bob_ids)
        self.assertIn(shared["task_id"], bob_ids)
        self.assertNotIn(alice_id, bob_ids)

        # Own detail: the full readable report projection.
        status, own, _ = self.board.request(
            "GET", "/api/v1/tasks/" + alice_id, cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 200)
        self.assertEqual(own["task_id"], alice_id)
        self.assertTrue(own["input_versions"])
        self.assertIn("output", own)

        # Foreign detail: the SAME answer as a missing id — no existence,
        # summary, report, input version or document disclosure.
        status, foreign, _ = self.board.request(
            "GET", "/api/v1/tasks/" + bob_id, cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 404)
        self.assertEqual(foreign, {"error": "unknown task_id"})
        body = json.dumps(foreign, ensure_ascii=False)
        for forbidden in ("output", "input_versions", "research_question",
                          "evidence_delivery", "BOB 的机密研究"):
            self.assertNotIn(forbidden, body)
        status, missing, _ = self.board.request(
            "GET", "/api/v1/tasks/task_does_not_exist", cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 404)
        self.assertEqual(missing, foreign, "foreign and missing must be indistinguishable")

        # Shared (unbound) research stays readable — compatibility path.
        status, shared_detail, _ = self.board.request(
            "GET", "/api/v1/tasks/" + shared["task_id"], cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 200)

    def test_board_first_screen_projections_are_scoped(self):
        bob_id = self._create_personal(self.bob_cookie, "BOB 待确认的问题？")
        claimed = self.store.claim_next("w-proj")
        self.assertEqual(claimed["task_id"], bob_id)
        from stock_agent.research_worker import run_research_task

        run_research_task(self.store, bob_id, worker_id="w-proj", config=self.config)

        for path in ("/api/v1/overview", "/api/v1/todo", "/api/v1/ops",
                     "/api/v1/events-view"):
            with self.subTest(path=path):
                status, payload, _ = self.board.request(
                    "GET", path, cookie=self.alice_cookie, csrf=False)
                self.assertEqual(status, 200)
                text = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(bob_id, text, "another session's task id leaked")
                self.assertNotIn("BOB 待确认的问题", text)

        # First paint itself carries no foreign row.
        status, page, _ = self.board.request("GET", "/", cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 200)
        self.assertNotIn(bob_id, page)

    # ---------------- realtime list ---------------- #

    def test_realtime_stream_filters_foreign_task_events(self):
        self._create_personal(self.alice_cookie, "ALICE 的实时研究？")
        cursor = max(event["sequence"] for event in self.store.events(limit=1000))
        conn, response = self.board.sse(cookie=self.alice_cookie, last_event_id=cursor)
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
        try:
            bob_id = self._create_personal(self.bob_cookie, "BOB 的实时研究？")
            alice_id = self._create_personal(self.alice_cookie, "ALICE 的实时研究 2？")
            seen = []
            while True:
                event = _read_sse_event(response)
                seen.append(event["data"].get("task_id"))
                if event["data"].get("task_id") == alice_id:
                    break
            self.assertNotIn(bob_id, seen, "a foreign task event reached the stream")
        finally:
            conn.close()

    def _create_for_alice_directly(self, question):
        """An ALICE-owned task created WITHOUT the HTTP session (the session
        is revoked/expired in these tests, but the event is still ALICE's)."""
        from stock_agent.personal_workspace import create_personal_research

        result = create_personal_research(
            self.store, actor=ALICE, question=question,
            symbols=["600519"], config=self.config)
        return result["task"]["task_id"]

    def _assert_stream_closes_without_events(self, response, deadline_seconds=6.0):
        """Read until EOF; any delivered event fails (post-revocation push)."""
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            line = response.readline()
            if line == b"":
                return
            if line.decode("utf-8", "replace").startswith("data:"):
                self.fail("the open stream pushed an event after its session ended")
        self.fail("the open stream did not close after its session ended")

    def test_open_stream_stops_after_session_revocation(self):
        self._create_personal(self.alice_cookie, "ALICE 撤销前研究？")
        cursor = max(event["sequence"] for event in self.store.events(limit=1000))
        conn, response = self.board.sse(cookie=self.alice_cookie, last_event_id=cursor)
        self.assertEqual(response.status, 200)
        try:
            # The stream is genuinely open and authorized for ALICE.
            live_id = self._create_personal(self.alice_cookie, "ALICE 撤销前实时事件？")
            event = _read_sse_event(response)
            self.assertEqual(event["data"].get("task_id"), live_id)

            status, _, _ = self.board.request(
                "POST", "/api/v1/personal/logout", {}, cookie=self.alice_cookie)
            self.assertEqual(status, 200)

            # This event belongs to ALICE and would have reached the old
            # scope; A48 says the open stream must stop instead.
            self._create_for_alice_directly("ALICE 撤销后不应推送？")
            self._assert_stream_closes_without_events(response)
        finally:
            conn.close()

    def test_open_stream_stops_after_session_expiry(self):
        self._create_personal(self.alice_cookie, "ALICE 过期前研究？")
        cursor = max(event["sequence"] for event in self.store.events(limit=1000))
        conn, response = self.board.sse(cookie=self.alice_cookie, last_event_id=cursor)
        self.assertEqual(response.status, 200)
        try:
            live_id = self._create_personal(self.alice_cookie, "ALICE 过期前实时事件？")
            event = _read_sse_event(response)
            self.assertEqual(event["data"].get("task_id"), live_id)

            # Move the server-side clock past the 900s session TTL: resolve()
            # now rejects the session, and the OPEN stream must honor it.
            self.clock_now[0] = self.clock_now[0] + timedelta(seconds=901)
            self._create_for_alice_directly("ALICE 过期后不应推送？")
            self._assert_stream_closes_without_events(response)
        finally:
            conn.close()

    def test_duplicate_create_cannot_hand_back_another_owners_case(self):
        # Question identity is global (question + scope + mode): a matching
        # submission must not return another owner's task as a "duplicate".
        alice_id = self._create_personal(self.alice_cookie, "同名问题的归属？")
        before = self._counts()
        status, body, _ = self.board.request(
            "POST", "/api/v1/workspace/create",
            {"question": "同名问题的归属？", "symbols": ["600519"]},
            cookie=self.bob_cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "not_owner")
        self.assertNotIn(alice_id, json.dumps(body, ensure_ascii=False))
        self.assertEqual(self._counts(), before)

        # The original owner's own retry still deduplicates to the same task.
        status, own_retry, _ = self.board.request(
            "POST", "/api/v1/workspace/create",
            {"question": "同名问题的归属？", "symbols": ["600519"]},
            cookie=self.alice_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(own_retry["outcome"], "DUPLICATE")
        self.assertEqual(own_retry["task_id"], alice_id)

    # ---------------- rerun write ownership ---------------- #

    def test_owner_can_rerun_with_prior_link_version_and_binding(self):
        original = self._finish(
            self._create_personal(self.alice_cookie, "ALICE 的旧案？"), actor=ALICE)
        status, body, _ = self.board.request(
            "POST", "/api/v1/workspace/rerun",
            {"task_id": original["task_id"]}, cookie=self.alice_cookie)
        self.assertEqual(status, 200, body)
        rerun_id = body["task_id"]
        self.assertNotEqual(rerun_id, original["task_id"])
        rerun = self.store.get_task(rerun_id)
        self.assertEqual(rerun["status"], "queued")
        self.assertEqual(rerun["input_version"], 1)
        self.assertEqual(rerun["input_payload"]["origin"]["prior_task_id"],
                         original["task_id"])
        binding = self.store.input_owner_binding(rerun_id)
        self.assertEqual(binding["owner"], ALICE)
        self.assertEqual(binding["via"], PERSONAL_ORIGIN_VIA)
        # The original terminal result is untouched.
        self.assertEqual(self.store.get_task(original["task_id"])["status"], "succeeded")

    def test_foreign_rerun_is_denied_without_side_effects(self):
        bob_task = self._finish(
            self._create_personal(self.bob_cookie, "BOB 的旧案？"), actor=BOB)
        before = self._counts()
        bob_snapshot = self.store.get_task(bob_task["task_id"])

        status, body, _ = self.board.request(
            "POST", "/api/v1/workspace/rerun",
            {"task_id": bob_task["task_id"]}, cookie=self.alice_cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "not_owner")
        self.assertNotIn("BOB 的旧案", json.dumps(body, ensure_ascii=False))

        after = self._counts()
        self.assertEqual(after, before,
                         "a denied rerun must create no task/binding/queue/model dispatch")
        self.assertEqual(self.store.get_task(bob_task["task_id"]), bob_snapshot)

    # ---------------- session rejection and compatibility ---------------- #

    def test_no_session_and_revoked_session_are_rejected(self):
        alice_id = self._create_personal(self.alice_cookie, "ALICE 的会话检查？")
        for path in ("/api/v1/tasks", "/api/v1/tasks/" + alice_id,
                     "/api/v1/overview", "/api/v1/todo", "/api/v1/ops",
                     "/api/v1/events-view"):
            with self.subTest(path=path):
                status, body, _ = self.board.request("GET", path, csrf=False)
                self.assertEqual(status, 403)
                self.assertEqual(body["error"], "session_required")

        # Realtime stream: no session means a JSON rejection, not a stream.
        conn, response = self.board.sse()
        try:
            self.assertEqual(response.status, 403)
            self.assertEqual(json.loads(response.read().decode("utf-8"))["error"],
                             "session_required")
        finally:
            conn.close()

        # Revoked session fails on the NEXT read, exactly like a write.
        status, body, _ = self.board.request(
            "POST", "/api/v1/personal/logout", {}, cookie=self.alice_cookie)
        self.assertEqual(status, 200)
        status, body, _ = self.board.request(
            "GET", "/api/v1/tasks", cookie=self.alice_cookie, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "session_required")

    def test_service_identity_cannot_obtain_a_session(self):
        for actor in ("dify:cred-1", "service_backup", "svc-relay", "system:cron"):
            with self.subTest(actor=actor):
                status, body, set_cookie = self.board.request(
                    "POST", "/api/v1/personal/login",
                    {"actor": actor, "symbols": ["600519"]})
                self.assertEqual(status, 403)
                self.assertEqual(body["error"], "login_failed")
                self.assertNotIn("task_board_session", set_cookie)

    def test_unbound_legacy_case_stays_shared_and_rerunnable(self):
        # A historical personal case from before the binding table: it has no
        # trusted binding, so it is treated as SHARED research (the documented
        # compatibility path) — readable and rerunnable by both sessions.
        from stock_agent.task_intake import ensure_question_task

        legacy_result = ensure_question_task(
            self.store, research_question="旧个人案的问题？", symbols=["600519"],
            config=self.config, actor=ALICE, execution_mode="replay")
        legacy = self._finish(legacy_result["task"]["task_id"], actor=ALICE)
        self.assertIsNone(self.store.input_owner_binding(legacy["task_id"]))

        for cookie in (self.alice_cookie, self.bob_cookie):
            status, detail, _ = self.board.request(
                "GET", "/api/v1/tasks/" + legacy["task_id"], cookie=cookie, csrf=False)
            self.assertEqual(status, 200)

        status, body, _ = self.board.request(
            "POST", "/api/v1/workspace/rerun",
            {"task_id": legacy["task_id"]}, cookie=self.bob_cookie)
        self.assertEqual(status, 200, body)
        rerun = self.store.get_task(body["task_id"])
        self.assertEqual(rerun["input_payload"]["origin"]["prior_task_id"],
                         legacy["task_id"])
        self.assertEqual(self.store.input_owner_binding(body["task_id"])["owner"], BOB)

    def test_binding_with_a_non_personal_via_is_still_shared(self):
        task, _ = self.store.create_task(
            title="流水线任务", input_payload=_spec("流水线研究？", self.config),
            execution_mode="replay",
            input_owner_binding={"owner": ALICE, "via": "pipeline"})
        status, detail, _ = self.board.request(
            "GET", "/api/v1/tasks/" + task["task_id"], cookie=self.bob_cookie, csrf=False)
        self.assertEqual(status, 200,
                         "only the personal workspace 'via' marks a task personal")

    def test_legacy_no_session_store_keeps_unscoped_reads(self):
        # Rollback path: without a session store the board keeps its original
        # unscoped reads (documented compatibility), so old deployments and
        # the CLI-driven Windows bridge are not broken by this change.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with project_path_override(lambda value: root / value):
            config = vault_config(root, {})
            store = TaskStore(root / "tasks.sqlite3")
            self.addCleanup(store.close)
            legacy_board = _Board(store, config, sessions=False)
            self.addCleanup(legacy_board.close)
            task, _ = store.create_task(
                title="遗留任务", input_payload=_spec("遗留共享研究？", config),
                execution_mode="replay",
                input_owner_binding={"owner": ALICE, "via": PERSONAL_ORIGIN_VIA})
            status, state, _ = legacy_board.request("GET", "/api/v1/tasks", csrf=False)
            self.assertEqual(status, 200)
            self.assertEqual([item["task_id"] for item in state["tasks"]],
                             [task["task_id"]])
            status, detail, _ = legacy_board.request(
                "GET", "/api/v1/tasks/" + task["task_id"], csrf=False)
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
