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

    def test_a93_create_attaches_documents_as_new_input_version(self):
        cookie = self._login()
        status, imported, _ = self._import_sample(cookie)
        document_id = imported["document"]["document_id"]
        status, created, _ = self._request(
            "POST", "/api/v1/workspace/create",
            {"question": "A93 页面接线的样本研究问题？", "symbols": ["600519"],
             "document_ids": [document_id]},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(created["outcome"], "RECORDED")
        task_id = created["task_id"]
        task = self.store.get_task(task_id)
        self.assertEqual(task["input_version"], 2,
                         "attaching the selected source is a governed new input version")
        summaries = task["input_payload"]["public_context"]["evidence_summaries"]
        self.assertIn(document_id, summaries)
        self.assertEqual(summaries[document_id]["sha256"],
                         imported["document"]["sha256"])
        snapshots = self.store.input_snapshots(task_id)
        self.assertEqual([item["input_version"] for item in snapshots], [1, 2],
                         "v1 (question) and v2 (attached source) both preserved")
        self.assertEqual(snapshots[1]["reason"], "attach_personal_documents")

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
        self.assertEqual(self.store.get_task(first["task_id"])["input_version"], 2)

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
        self.assertEqual([item["input_version"] for item in versions], [1, 2])
        self.assertEqual(versions[0]["reason"], "created")


if __name__ == "__main__":
    unittest.main()
