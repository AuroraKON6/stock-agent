"""R14 browser wiring (A93 O-half): the page-facing HTTP surface.

R14 delivered the four workspace actions; the board PAGE wiring they were
waiting on (login bar, personal source import, document selection at create,
plain-language feedback, cancel/rerun row buttons) lands with this batch.
These tests lock the HTTP contract the isolated browser acceptance drives:

- ``GET /api/v1/personal/session`` — read-only login state for the page;
- ``GET /api/v1/personal/documents`` — the owner's R12a namespace list;
- ``POST /api/v1/personal/import`` — session + CSRF gated upload into the
  personal namespace (never the institutional vault);
- ``POST /api/v1/workspace/create`` with ``document_ids`` — selected sources
  attach as a governed NEW input version (snapshot chain preserved);
- session-mode SSR renders the personal panel and plain-language feedback,
  while the legacy (no-session) page keeps its original form untouched.
"""
import base64
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.identity_providers import PersonalSessionStore
from stock_agent.personal_evidence import personal_import_audit
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

OWNER = "browser-wiring-user"
SAMPLE_ANNOUNCEMENT = """<!DOCTYPE html><html><head><title>A93样本公告</title></head>
<body><h1>样本公司样本公告</h1>
<p>本公告为 R14 页面接线验收的明示样本文本，不是任何真实公司的披露文件。</p>
<p>营业收入为 999.99 万元。</p>
</body></html>"""


class BrowserWiringTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        # The personal namespace root must be ABSOLUTE: board handler threads
        # do not inherit the test's project_path override (contextvar), so a
        # relative root would resolve against the real workspace there.
        self.config["personal_evidence"] = {"root": str(self.root / "personal_evidence")}
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

        def authenticator(config, actor, symbols):
            if actor != OWNER:
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-a93-wiring", "policy_sha256": "a" * 64}

        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=PersonalSessionStore(ttl_seconds=1800),
            login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    # ---------------- helpers ---------------- #

    def _request(self, method, path, body=None, *, cookie=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {}
            payload = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                payload = json.dumps(body)
            if csrf:
                headers["X-Task-Board-CSRF"] = str(self.server.csrf_token)
            if cookie is not None:
                headers["Cookie"] = cookie
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            data = response.read().decode("utf-8")
            try:
                return response.status, json.loads(data), dict(response.getheaders())
            except json.JSONDecodeError:
                return response.status, {"raw": data}, dict(response.getheaders())
        finally:
            conn.close()

    def _login(self):
        status, body, headers = self._request(
            "POST", "/api/v1/personal/login",
            {"actor": OWNER, "symbols": ["600519"]}, csrf=False)
        self.assertEqual(status, 200)
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        self.assertTrue(cookie)
        return cookie

    def _import_sample(self, cookie, title="A93样本公告（非真实披露）"):
        return self._request(
            "POST", "/api/v1/personal/import",
            {"filename": "announcement.html",
             "content_base64": base64.b64encode(SAMPLE_ANNOUNCEMENT.encode("utf-8")).decode("ascii"),
             "source_url": "https://www.cninfo.com.cn/new/disclosure/detail?plate=szse&fixture=a93-wiring",
             "title": title,
             "published_at": "2026-11-22T18:00:00+08:00"},
            cookie=cookie)

    # ---------------- session state ---------------- #

    def test_a93_session_route_reports_login_state(self):
        status, body, _ = self._request("GET", "/api/v1/personal/session", csrf=False)
        self.assertEqual(status, 200)
        self.assertFalse(body["logged_in"])
        self.assertTrue(body["sessions_available"])
        cookie = self._login()
        status, body, _ = self._request("GET", "/api/v1/personal/session",
                                        csrf=False, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body["logged_in"])
        self.assertEqual(body["actor"], OWNER)

    def test_a93_documents_and_import_require_session(self):
        status, body, _ = self._request("GET", "/api/v1/personal/documents", csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "session_required")
        # CSRF is checked BEFORE the session (same gate order as the other
        # board POSTs): a valid CSRF token without a session gets the honest
        # session_required refusal.
        status, body, _ = self._request("POST", "/api/v1/personal/import",
                                        {"filename": "x.html", "content_base64": "eA==",
                                         "source_url": "https://www.cninfo.com.cn/a", "title": "t",
                                         "published_at": "2026-11-22T18:00:00+08:00"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "session_required")
        status, body, _ = self._request("POST", "/api/v1/personal/import",
                                        {"filename": "x.html", "content_base64": "eA==",
                                         "source_url": "https://www.cninfo.com.cn/a", "title": "t",
                                         "published_at": "2026-11-22T18:00:00+08:00"},
                                        csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "csrf validation failed")

    def test_a93_import_rejects_bad_csrf_and_bad_payload(self):
        cookie = self._login()
        status, body, _ = self._request("POST", "/api/v1/personal/import",
                                        {"filename": "x.html", "content_base64": "eA==",
                                         "source_url": "https://example.com/a", "title": "t",
                                         "published_at": "2026-11-22T18:00:00+08:00"},
                                        cookie=cookie, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "csrf validation failed")
        status, body, _ = self._request("POST", "/api/v1/personal/import",
                                        {"filename": "x.html", "content_base64": "!!!",
                                         "source_url": "https://example.com/a", "title": "t",
                                         "published_at": "2026-11-22T18:00:00+08:00"},
                                        cookie=cookie)
        self.assertEqual(status, 400)
        status, body, _ = self._request("POST", "/api/v1/personal/import",
                                        {"filename": "x.html", "content_base64": "eA==",
                                         "source_url": "http://not-https.example/a", "title": "t",
                                         "published_at": "2026-11-22T18:00:00+08:00"},
                                        cookie=cookie)
        self.assertEqual(status, 400)

    def test_a93_import_into_personal_namespace_and_list(self):
        cookie = self._login()
        status, body, _ = self._import_sample(cookie)
        self.assertEqual(status, 200)
        document = body["document"]
        self.assertTrue(document["document_id"])
        self.assertIn("sha256", document)
        status, listing, _ = self._request("GET", "/api/v1/personal/documents",
                                           csrf=False, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(listing["owner"], OWNER)
        self.assertEqual(listing["namespace"], "personal_research")
        listed = {item["document_id"]: item for item in listing["documents"]}
        self.assertIn(document["document_id"], listed)
        audit = personal_import_audit(self.config, OWNER)
        self.assertEqual(audit[-1]["institutional_admission"], "NOT_REQUESTED")
        self.assertEqual(audit[-1]["owner"], OWNER)

    # ---------------- create with documents ---------------- #

    def test_a93_create_commits_complete_input_before_claimability(self):
        cookie = self._login()
        status, imported, _ = self._import_sample(cookie)
        document_id = imported["document"]["document_id"]
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "首次领取即可见原件的样本问题？", "symbols": ["600519"],
             "document_ids": [document_id]},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(created["outcome"], "RECORDED")
        task_id = created["task_id"]
        task = self.store.get_task(task_id)
        summaries = task["input_payload"]["public_context"]["evidence_summaries"]
        self.assertIn(document_id, summaries)
        self.assertEqual(summaries[document_id]["sha256"],
                         imported["document"]["sha256"])
        # Independent connection: once the create response is back, the very
        # FIRST claim already sees the selected source — the complete input
        # committed together with the claimable state (no create→bump window).
        second = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(second.close)
        claimed = second.claim_next("w-concurrent")
        self.assertEqual(claimed["task_id"], task_id)
        self.assertIn(document_id,
                      claimed["input_payload"]["public_context"]["evidence_summaries"])
        self.assertEqual(claimed["input_version"], 1,
                         "the task is born with its complete v1 input")
        snapshots = self.store.input_snapshots(task_id)
        self.assertEqual([item["input_version"] for item in snapshots], [1],
                         "the R01 snapshot chain starts at the complete v1")
        self.assertEqual(sorted((snapshots[0]["payload"].get("public_context") or {})
                                .get("evidence_summaries") or {}), [document_id],
                         "the v1 snapshot itself carries the selected source")

    def test_a93_create_rejects_unknown_document(self):
        cookie = self._login()
        status, body, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "引用不存在原件的问题？", "symbols": ["600519"],
             "document_ids": ["doc_does_not_exist"]},
            cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_document")

    def test_a93_duplicate_create_does_not_bump_version(self):
        cookie = self._login()
        status, imported, _ = self._import_sample(cookie)
        document_id = imported["document"]["document_id"]
        payload = {"question": "重复创建的样本问题？", "symbols": ["600519"],
                   "document_ids": [document_id]}
        status, first, _ = self._request("POST", "/api/v1/workspace/create",
                                         payload, cookie=cookie)
        self.assertEqual(status, 200)
        status, second, _ = self._request("POST", "/api/v1/workspace/create",
                                          payload, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(second["outcome"], "DUPLICATE")
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.store.get_task(first["task_id"])["input_version"], 1)

    # ---------------- page rendering ---------------- #

    def _get_page(self, *, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Cookie": cookie} if cookie else {}
            conn.request("GET", "/", None, headers)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def test_a93_session_mode_page_renders_personal_panel(self):
        status, page = self._get_page()
        self.assertEqual(status, 200)
        self.assertIn("var SESSION_MODE = true;", page)
        self.assertIn("登录个人工作台", page,
                      "logged-out session page server-renders the login form")
        cookie = self._login()
        status, page = self._get_page(cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("已登录：{0}".format(OWNER), page)
        self.assertIn("创建研究（replay 样本回放）", page)
        self.assertIn("导入样本原件", page)

    def test_a93_awaiting_row_uses_plain_language_feedback_in_session_mode(self):
        from stock_agent.task_board import STATUS_AWAITING_HUMAN, _render_html

        state = {"schema_version": "1.0", "generated_at": "", "summary": {},
                 "tasks": [{"task_id": "task_a93", "is_replay": True,
                            "status": STATUS_AWAITING_HUMAN, "title": "样本任务",
                            "input_version": 1, "version": 3, "model_call": {},
                            "last_error": "",
                            "human_request": {"question": "是否接受该回放草稿？"}}]}
        session_page = _render_html(state, "csrf", session_mode=True, session_actor=OWNER)
        form_start = session_page.index('data-taskform="task_a93"')
        form_html = session_page[form_start:session_page.index("</form>", form_start)]
        self.assertIn("提交追问/补证", form_html)
        self.assertIn("签收", form_html)
        self.assertIn("否定", form_html)
        self.assertNotIn('name="actor"', form_html,
                         "plain-language feedback must not ask for an actor")
        self.assertNotIn("JSON patch", form_html)
        self.assertIn("取消", session_page[session_page.index('data-task="task_a93"'):form_start],
                      "unfinished rows expose the cancel action")
        legacy_page = _render_html(state, "csrf")
        legacy_form = legacy_page[legacy_page.index('data-taskform="task_a93"'):]
        legacy_form = legacy_form[:legacy_form.index("</form>")]
        self.assertIn('name="actor"', legacy_form,
                      "legacy pages keep the low-level feedback form")

    def test_a93_legacy_page_keeps_original_feedback_form(self):
        legacy = create_task_board_server(self.store, host="127.0.0.1", port=0,
                                          config=self.config)
        self.addCleanup(legacy.server_close)
        threading.Thread(target=legacy.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", legacy.server_address[1], timeout=10)
        try:
            conn.request("GET", "/")
            page = conn.getresponse().read().decode("utf-8")
        finally:
            conn.close()
        self.assertIn('name="actor" placeholder="actor"', page,
                      "legacy (no-session) rollback page keeps its form")
        self.assertIn("var SESSION_MODE = false;", page,
                      "legacy pages never render the personal panel")
        self.assertNotIn("__SESSION_MODE_JSON__", page,
                         "all template placeholders must be substituted")

    def test_a93_task_detail_lists_input_versions(self):
        cookie = self._login()
        status, imported, _ = self._import_sample(cookie)
        document_id = imported["document"]["document_id"]
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "版本可读性的样本问题？", "symbols": ["600519"],
             "document_ids": [document_id]},
            cookie=cookie)
        task_id = created["task_id"]
        status, detail, _ = self._request("GET", "/api/v1/tasks/{0}".format(task_id),
                                          csrf=False, cookie=cookie)
        self.assertEqual(status, 200)
        versions = detail["input_versions"]
        self.assertEqual([item["input_version"] for item in versions], [1])
        self.assertEqual(versions[0]["reason"], "created")

    # ---------------- rework: failed create leaves no orphan ---------------- #

    def test_a93_failed_create_leaves_queue_empty_then_retry_creates_one(self):
        cookie = self._login()
        question = "失败重试语义的样本问题？"
        status, body, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": question, "symbols": ["600519"],
             "document_ids": ["doc_does_not_exist"]},
            cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_document")
        self.assertEqual(self.store.list_tasks(), [],
                         "a failed create must leave the queue untouched")
        status, imported, _ = self._import_sample(cookie)
        document_id = imported["document"]["document_id"]
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": question, "symbols": ["600519"],
             "document_ids": [document_id]},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(created["outcome"], "RECORDED")
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1, "retry creates exactly ONE task")
        payload = tasks[0]["input_payload"]
        self.assertIn(document_id, payload["public_context"]["evidence_summaries"],
                      "the worker reads the selected source on first claim")

    # ---------------- rework: A90 no-question completion ---------------- #

    def _succeeded_no_question_task(self, cookie, question):
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": question, "symbols": ["600519"]}, cookie=cookie)
        self.assertEqual(status, 200)
        task_id = created["task_id"]
        claimed = self.store.claim_next("w-o-layer")
        self.assertEqual(claimed["task_id"], task_id)
        # Fixture prep through the GOVERNED version bump (auditable, no raw
        # SQL): the recorded replay answer carries no human question.
        payload = self.store.get_task(task_id)["input_payload"]
        replay = dict(payload.get("replay_response") or {})
        replay["human_questions"] = []
        self.store.bump_input_version(
            task_id, payload_overrides={"replay_response": replay},
            actor="fixture-replay", reason="fixture: replay without human questions")
        from stock_agent.research_worker import run_research_task

        result = run_research_task(self.store, task_id, worker_id="w-o-layer",
                                   config=self.config)
        self.assertEqual(result["status"], "succeeded")
        return task_id

    def test_a90_followup_on_no_question_completion_creates_linked_task(self):
        cookie = self._login()
        task_id = self._succeeded_no_question_task(cookie, "A90 无待答问题的样本研究？")
        before = self.store.get_task(task_id)
        before_output = before["output"]
        followup_note = "主动追问：样本结论对下游口径是否稳健？"
        status, followed, _ = self._request(
            "POST", "/api/v1/workspace/followup",
            {"task_id": task_id, "expected_version": before["version"],
             "extra_note": followup_note},
            cookie=cookie)
        self.assertEqual(status, 200)
        new_id = followed["task_id"]
        self.assertNotEqual(new_id, task_id)
        new_task = self.store.get_task(new_id)
        origin = new_task["input_payload"]["origin"]
        self.assertEqual(origin["prior_task_id"], task_id)
        self.assertEqual(origin["research_occasion"], "user_followup")
        self.assertEqual(origin["followup_note"], followup_note)
        self.assertEqual(new_task["input_payload"]["research_question"], followup_note,
                         "the follow-up text is readable back as the new question")
        original = self.store.get_task(task_id)
        self.assertEqual(original["status"], "succeeded")
        self.assertEqual(original["output"], before_output,
                         "the finished result is preserved byte-for-byte")

    def test_a90_followup_task_claimable_only_with_complete_origin(self):
        cookie = self._login()
        task_id = self._succeeded_no_question_task(cookie, "A90 追问领取完整性的样本研究？")
        task = self.store.get_task(task_id)
        status, followed, _ = self._request(
            "POST", "/api/v1/workspace/followup",
            {"task_id": task_id, "expected_version": task["version"],
             "extra_note": "主动追问：领取时前案链接必须已完整。"},
            cookie=cookie)
        self.assertEqual(status, 200)
        # Independent connection: the FIRST claim of the follow-up task
        # already sees the correct prior link and occasion — they rode the
        # create transaction, not a later correction bump.
        second = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(second.close)
        claimed = second.claim_next("w-concurrent")
        self.assertEqual(claimed["task_id"], followed["task_id"])
        origin = claimed["input_payload"]["origin"]
        self.assertEqual(origin["prior_task_id"], task_id)
        self.assertEqual(origin["research_occasion"], "user_followup")
        self.assertEqual(claimed["input_version"], 1,
                         "the follow-up task is born complete (no correction bump)")

    def test_a90_followup_retry_with_same_command_reuses_task(self):
        cookie = self._login()
        task_id = self._succeeded_no_question_task(cookie, "A90 追问重试去重的样本研究？")
        task = self.store.get_task(task_id)
        body = {"task_id": task_id, "expected_version": task["version"],
                "extra_note": "主动追问：响应丢失后重发应复用同一任务。",
                "command_id": "cmd-followup-retry-1"}
        status, first, _ = self._request("POST", "/api/v1/workspace/followup",
                                         body, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(first["outcome"], "RECORDED")
        # The first submission succeeded but its response was lost: the page
        # resends the SAME request id — one task, not two.
        status, retry, _ = self._request("POST", "/api/v1/workspace/followup",
                                         body, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(retry["outcome"], "DUPLICATE")
        self.assertEqual(retry["task_id"], first["task_id"])
        followups = [item for item in self.store.list_tasks()
                     if item["task_id"] != task_id]
        self.assertEqual(len(followups), 1, "the retry queued no second task")
        # A genuinely new user action (fresh request id) still creates new work.
        status, anew, _ = self._request(
            "POST", "/api/v1/workspace/followup",
            {"task_id": task_id, "expected_version": task["version"],
             "extra_note": "主动追问：这是用户另行发起的第二次追问。",
             "command_id": "cmd-followup-retry-2"},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(anew["outcome"], "RECORDED")
        self.assertNotEqual(anew["task_id"], first["task_id"])

    def test_a90_signoff_on_no_question_completion(self):
        cookie = self._login()
        task_id = self._succeeded_no_question_task(cookie, "A90 签收无待答结果的样本研究？")
        task = self.store.get_task(task_id)
        summary_before = task["output"].get("summary")
        status, signed, _ = self._request(
            "POST", "/api/v1/workspace/acknowledge",
            {"task_id": task_id, "expected_version": task["version"],
             "notes": "样本签收（页面验收）"},
            cookie=cookie)
        self.assertEqual(status, 200)
        after = self.store.get_task(task_id)
        self.assertEqual(after["status"], "succeeded")
        acknowledgement = after["output"]["personal_acknowledgement"]
        self.assertEqual(acknowledgement["actor"], OWNER)
        self.assertEqual(acknowledgement["scope"],
                         "personal_research_acknowledgement_not_institutional_approval")
        self.assertEqual(after["output"].get("summary"), summary_before,
                         "sign-off adds the record without rewriting the output")
        status, again, _ = self._request(
            "POST", "/api/v1/workspace/acknowledge",
            {"task_id": task_id, "expected_version": after["version"]},
            cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(again["error"], "already_acknowledged")

    def test_a90_signoff_rejects_unfinished_tasks(self):
        cookie = self._login()
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "签收门控的样本问题？", "symbols": ["600519"]}, cookie=cookie)
        task_id = created["task_id"]
        status, body, _ = self._request(
            "POST", "/api/v1/workspace/acknowledge",
            {"task_id": task_id, "expected_version": 1}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "not_succeeded")

    # ---------------- rework: A67 version reads back the feedback ---------------- #

    def test_a67_versions_expose_followup_note_and_selected_documents(self):
        cookie = self._login()
        status, imported, _ = self._import_sample(cookie)
        document_id = imported["document"]["document_id"]
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "版本读回的样本问题？", "symbols": ["600519"],
             "document_ids": [document_id]},
            cookie=cookie)
        task_id = created["task_id"]
        claimed = self.store.claim_next("w-o-layer")
        self.assertEqual(claimed["task_id"], task_id)
        from stock_agent.research_worker import run_research_task

        result = run_research_task(self.store, task_id, worker_id="w-o-layer",
                                   config=self.config)
        self.assertEqual(result["status"], "awaiting_human")
        awaiting = self.store.get_task(task_id)
        note = "版本读回：请补充分季度口径。"
        status, _, _ = self._request(
            "POST", "/api/v1/workspace/followup",
            {"task_id": task_id, "expected_version": awaiting["version"],
             "extra_note": note},
            cookie=cookie)
        self.assertEqual(status, 200)
        status, detail, _ = self._request("GET", "/api/v1/tasks/{0}".format(task_id),
                                          csrf=False, cookie=cookie)
        self.assertEqual(status, 200)
        versions = {item["input_version"]: item for item in detail["input_versions"]}
        self.assertEqual(sorted(versions), [1, 2])
        self.assertEqual(versions[1]["evidence_documents"], [document_id],
                         "the creation version already names the selected document")
        self.assertEqual(versions[1]["reason"], "created")
        self.assertEqual(versions[2]["followup_note"], note,
                         "the typed follow-up is readable at its version")
        self.assertEqual(versions[2]["reason"], "human_provide_input")
        self.assertEqual(versions[2]["evidence_documents"], [document_id],
                         "the follow-up version still names the selected document")

    # ---------------- rework: buttons match state and session ---------------- #

    def _render_with(self, status, *, output=None, session_mode=True, session_actor=OWNER):
        from stock_agent.task_board import _render_html

        state = {"schema_version": "1.0", "generated_at": "", "summary": {},
                 "tasks": [{"task_id": "task_btn", "is_replay": True,
                            "status": status, "title": "按钮态样本",
                            "input_version": 1, "version": 4, "model_call": {},
                            "last_error": "", "human_request": {},
                            "output": output or {}}]}
        page = _render_html(state, "csrf", session_mode=session_mode,
                            session_actor=session_actor)
        row_start = page.index('data-task="task_btn"')
        return page, page[row_start:page.index("</tr>", row_start)]

    def test_buttons_blocked_row_offers_rerun_not_cancel(self):
        page, row = self._render_with("blocked")
        self.assertNotIn('data-ws="cancel"', row,
                         "blocked is terminal — the store would refuse the cancel")
        self.assertIn('data-ws="rerun"', row)

    def test_buttons_succeeded_unacknowledged_offers_signoff_and_followup(self):
        page, row_and_form = self._render_with("succeeded")
        self.assertIn('data-ws="signoff"', page)
        self.assertIn('data-ws="followup"', page)
        acknowledged = self._render_with(
            "succeeded", output={"personal_acknowledgement": {"actor": OWNER}})[0]
        acknowledged_markup = acknowledged.split("<script>")[0]
        self.assertNotIn('data-ws="signoff"', acknowledged_markup,
                         "an already-signed result hides the sign-off button")
        self.assertIn('data-ws="followup"', acknowledged_markup)

    def test_buttons_logged_out_and_legacy_show_no_session_actions(self):
        page, row = self._render_with("queued", session_mode=True, session_actor="")
        self.assertNotIn("data-ws=", row,
                         "logged-out pages show no session-gated action buttons")
        from stock_agent.task_board import _render_html

        state = {"schema_version": "1.0", "generated_at": "", "summary": {},
                 "tasks": [{"task_id": "task_btn", "is_replay": True,
                            "status": "awaiting_human", "title": "t",
                            "input_version": 1, "version": 4,
                            "model_call": {}, "last_error": "",
                            "human_request": {"question": "样本问题？"}}]}
        # Assert on the SSR MARKUP only — the template's JS source always
        # contains the class/button strings for the client renderer.
        logged_out = _render_html(state, "csrf", session_mode=True, session_actor="")
        logged_out_markup = logged_out.split("<script>")[0]
        self.assertNotIn("<tr data-taskform", logged_out_markup,
                         "logged-out session pages render no feedback form row")
        self.assertNotIn('placeholder="actor"', logged_out_markup,
                         "the legacy feedback form never appears on session pages")
        legacy = _render_html(state, "csrf")
        legacy_markup = legacy.split("<script>")[0]
        self.assertIn('name="actor" placeholder="actor"', legacy_markup,
                      "legacy rollback pages keep their form")
        self.assertNotIn("data-ws=", legacy_markup)


if __name__ == "__main__":
    unittest.main()
