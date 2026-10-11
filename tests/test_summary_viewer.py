"""Read-only scoped viewer with synthetic accounts and real loopback HTTP."""
import contextlib
import copy
import http.client
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from stock_agent.local_identity import LocalIdentityError
from stock_agent.summary_viewer import SummaryViewAccess, SummaryViewerError, create_summary_view_server, serve_summary_viewer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _summary_fixture import SummaryFixture, AUTHOR, REVIEWER, SID_A, SID_R


class SummaryViewerTests(SummaryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.draft = self.submit()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def access(self, **overrides):
        args = dict(config_path=str(self.config_path), task_id=self.task_id, actor=REVIEWER,
                    draft_id=self.draft["draft_id"], expected_version=self.version())
        args.update(overrides)
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R):
            return SummaryViewAccess(**args)

    def server(self, **overrides):
        args = dict(config_path=str(self.config_path), task_id=self.task_id, actor=REVIEWER,
                    draft_id=self.draft["draft_id"], expected_version=self.version())
        args.update(overrides)
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R):
            server = create_summary_view_server(**args)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        return server

    def request(self, server, method, path, body=None, headers=None):
        defaults = {"Origin": server.origin, "X-Review-Request": "1", "Content-Type": "application/json"}
        defaults.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body, defaults)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def exchange(self, server):
        code, _, data = self.request(server, "POST", "/session", json.dumps({"bootstrap": server.access._bootstrap}))
        self.assertEqual(code, 200)
        return json.loads(data)["session_token"]

    def test_detail_is_exact_task_and_source_without_os_subjects_or_paths(self):
        access = self.access()
        detail = access.read_detail()
        self.assertEqual(detail["task"]["task_id"], self.task_id)
        self.assertEqual(detail["review"]["draft_id"], self.draft["draft_id"])
        self.assertEqual(detail["excerpts"][0]["text"], "Revenue decreased.")
        self.assertFalse(detail["review"]["model_called"])
        self.assertFalse(detail["review"]["institutional_approval"])
        serialized = json.dumps(detail)
        for secret in (SID_A, SID_R, str(self.root), "control:synthetic-only"):
            self.assertNotIn(secret, serialized)

    def test_start_requires_native_account_not_actor_name_and_binds_before_listen(self):
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_A), \
             patch("stock_agent.summary_viewer._ReviewServer") as constructor:
            with self.assertRaises(LocalIdentityError):
                create_summary_view_server(str(self.config_path), self.task_id, REVIEWER, self.draft["draft_id"], self.version())
        constructor.assert_not_called()

    def test_explicit_review_permission_and_enforcement_required(self):
        self.roster["principals"][1]["authorizations"] = []
        self.save_roster()
        with self.assertRaises(LocalIdentityError):
            self.access()

    def test_wrong_or_missing_draft_and_stale_version_denied(self):
        for args in ({"draft_id": "unknown"}, {"draft_id": ""}, {"expected_version": self.version() - 1}, {"expected_version": True}):
            with self.subTest(args=args), self.assertRaises(SummaryViewerError):
                self.access(**args)

    def test_unsubmitted_or_expired_draft_cannot_open(self):
        from datetime import timedelta
        from stock_agent.summary_review import _now
        with patch("stock_agent.summary_review._now", return_value=_now() + timedelta(hours=25)):
            with self.assertRaises(SummaryViewerError):
                self.access()

    def test_bootstrap_is_single_use_and_only_digest_of_session_is_retained(self):
        access = self.access()
        bootstrap = access._bootstrap
        result = access.exchange(bootstrap)
        access.authorize(result["session_token"])
        self.assertEqual(access._bootstrap, "")
        self.assertNotEqual(access._session_sha, result["session_token"])
        with self.assertRaises(SummaryViewerError):
            access.exchange(bootstrap)
        with self.assertRaises(SummaryViewerError):
            access.authorize(bootstrap)

    def test_sessions_expire_without_sliding_renewal_or_clock_rollback(self):
        access = self.access()
        token = access.exchange(access._bootstrap)["session_token"]
        deadline = access.deadline
        access.authorize(token)
        self.assertEqual(access.deadline, deadline)
        with patch("stock_agent.summary_viewer.time.monotonic", return_value=deadline):
            with self.assertRaises(SummaryViewerError):
                access.authorize(token)
        with self.assertRaises(SummaryViewerError):
            access.authorize(token)
        for lifetime in (0, 301, True):
            with self.subTest(lifetime=lifetime), self.assertRaises(SummaryViewerError):
                self.access(lifetime_seconds=lifetime)

    def test_role_revocation_is_terminal_even_if_roster_is_restored(self):
        access = self.access()
        original = copy.deepcopy(self.roster)
        self.roster["principals"][1]["active"] = False
        self.save_roster()
        with self.assertRaises(SummaryViewerError):
            access.read_detail()
        self.roster = original
        self.save_roster()
        with self.assertRaises(SummaryViewerError):
            access.read_detail()

    def test_restriction_change_invalidates_access(self):
        access = self.access()
        Path(self.config["research_operations"]["restrictions_path"]).write_text('{"entries":[],"revision":2}', encoding="utf-8")
        with self.assertRaises(SummaryViewerError):
            access.read_detail()

    def test_config_change_is_detected_without_loading_secrets(self):
        access = self.access()
        self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        with self.assertRaises(SummaryViewerError):
            access.read_detail()

    def test_source_tamper_is_never_returned_as_stale_content(self):
        access = self.access()
        obj = self.root / "vault/objects" / self.sha[:2] / self.sha
        obj.write_bytes(b"tampered source bytes")
        with self.assertRaises(SummaryViewerError):
            access.read_detail()

    def test_real_review_version_change_closes_existing_view(self):
        access = self.access()
        self.decide(self.draft)
        with self.assertRaises(SummaryViewerError):
            access.read_detail()
        new = self.access().read_detail()
        self.assertEqual(new["review"]["status"], "supported")
        self.assertEqual(new["history"][0]["actor"], REVIEWER)
        self.assertNotIn(SID_R, json.dumps(new))

    def test_http_anonymous_shell_has_no_data_or_secret_and_api_requires_token(self):
        server = self.server()
        for path in ("/api/detail", "/api/detail?task_id=other", "/?bootstrap=anything"):
            code, _, data = self.request(server, "GET", path)
            self.assertIn(code, (403, 404))
            self.assertNotIn(b"Revenue", data)
        code, headers, data = self.request(server, "GET", "/", headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(code, 200)
        self.assertNotIn(server.access._bootstrap.encode(), data)
        self.assertNotIn(self.task_id.encode(), data)
        self.assertNotIn(b"Synthetic", data)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Set-Cookie", headers)

    def test_http_same_host_wrong_port_and_cross_origin_denied(self):
        server = self.server()
        body = json.dumps({"bootstrap": server.access._bootstrap})
        for headers in ({"Origin": "http://127.0.0.1:1"}, {"Origin": "https://example.org"},
                        {"Origin": "null"}, {"Origin": ""}, {"Host": "evil.example"},
                        {"Host": "127.0.0.1:1"}, {"Sec-Fetch-Site": "cross-site"}, {"X-Review-Request": ""}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(server, "POST", "/session", body, headers)[0], 403)
        token = self.exchange(server)
        self.assertEqual(self.request(server, "GET", "/api/detail", headers={"Authorization": "Bearer " + token})[0], 200)

    def test_http_framing_and_actor_injection_rejected(self):
        server = self.server()
        bootstrap = server.access._bootstrap
        for body, headers in (("[]", {}), ("not-json", {}), ("{}", {}),
                              (json.dumps({"bootstrap": bootstrap, "actor": AUTHOR}), {}),
                              (json.dumps({"bootstrap": bootstrap}), {"Content-Type": "text/plain"}),
                              ("{}", {"Content-Length": "bad"}), ("{}", {"Transfer-Encoding": "chunked"})):
            with self.subTest(body=body):
                self.assertEqual(self.request(server, "POST", "/session", body, headers)[0], 400)
        self.exchange(server)

    def test_http_view_exchange_and_close_do_not_mutate_business_tables(self):
        before = self.store._conn.total_changes
        version = self.version()
        events = self.store.events(self.task_id)
        server = self.server()
        token = self.exchange(server)
        headers = {"Authorization": "Bearer " + token}
        code, _, body = self.request(server, "GET", "/api/detail", headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["review"]["summary_id"], self.draft["summary_id"])
        self.assertEqual(self.request(server, "POST", "/api/v1/summary-review", "{}", headers)[0], 404)
        self.assertEqual(self.request(server, "POST", "/api/v1/human-responses", "{}", headers)[0], 404)
        self.assertEqual(self.request(server, "POST", "/close", "{}", headers)[0], 200)
        self.assertEqual(self.request(server, "GET", "/api/detail", headers=headers)[0], 403)
        self.assertEqual(self.version(), version)
        self.assertEqual(self.store.events(self.task_id), events)
        self.assertEqual(self.store._conn.total_changes, before)

    def test_http_never_authenticates_browser_as_server_windows_account(self):
        server = self.server()
        with patch("stock_agent.local_identity.current_windows_subject", side_effect=AssertionError("must not use server SID")):
            token = self.exchange(server)
            self.assertEqual(self.request(server, "GET", "/api/detail", headers={"Authorization": "Bearer " + token})[0], 200)

    def test_http_role_revocation_removes_data_on_next_read(self):
        server = self.server()
        token = self.exchange(server)
        self.roster["principals"][1]["active"] = False
        self.save_roster()
        code, _, body = self.request(server, "GET", "/api/detail", headers={"Authorization": "Bearer " + token})
        self.assertEqual(code, 403)
        self.assertNotIn(b"Revenue", body)
        self.assertNotIn(str(self.root).encode(), body)

    def test_cli_rejects_before_secret_loading_directory_creation_or_browser_open(self):
        from stock_agent.cli import main
        with patch.object(sys, "argv", ["stock_agent", "task-summary-open", "--config", str(self.config_path),
             "--task-id", self.task_id, "--actor", REVIEWER, "--draft-id", self.draft["draft_id"], "--expected-version", str(self.version())]), \
             patch("stock_agent.local_identity.current_windows_subject", return_value=SID_A), \
             patch("stock_agent.config.load_local_secrets", side_effect=AssertionError("secrets")), \
             patch("stock_agent.cli.ensure_directories", side_effect=AssertionError("directories")), \
             patch("webbrowser.open") as browser:
            with self.assertRaisesRegex(SystemExit, "Local summary view rejected"):
                main()
        browser.assert_not_called()

    def test_failed_browser_launch_revokes_capability_without_printing_it(self):
        output = io.StringIO()
        captured = []
        from stock_agent import summary_viewer
        factory = summary_viewer.create_summary_view_server
        def capture(*args, **kwargs):
            server = factory(*args, **kwargs)
            captured.append(server)
            return server
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R), \
             patch("stock_agent.summary_viewer.create_summary_view_server", side_effect=capture), \
             patch("webbrowser.open", return_value=False), contextlib.redirect_stdout(output):
            with self.assertRaises(SummaryViewerError):
                serve_summary_viewer(str(self.config_path), self.task_id, REVIEWER, self.draft["draft_id"], self.version())
        self.assertTrue(captured[0].access.closed)
        self.assertEqual(captured[0].fileno(), -1)
        self.assertNotIn("#", output.getvalue())

    def test_cli_opens_only_after_authentication_and_ctrl_c_closes_socket(self):
        from stock_agent.cli import main
        from stock_agent import summary_viewer
        servers, urls = [], []
        factory = summary_viewer.create_summary_view_server
        def capture(*args, **kwargs):
            server = factory(*args, **kwargs)
            servers.append(server)
            return server
        output = io.StringIO()
        with patch.object(sys, "argv", ["stock_agent", "task-summary-open", "--config", str(self.config_path),
             "--task-id", self.task_id, "--actor", REVIEWER, "--draft-id", self.draft["draft_id"], "--expected-version", str(self.version())]), \
             patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R), \
             patch("stock_agent.config.load_local_secrets", side_effect=AssertionError("secrets")), \
             patch("stock_agent.cli.ensure_directories", side_effect=AssertionError("directories")), \
             patch("stock_agent.summary_viewer.create_summary_view_server", side_effect=capture), \
             patch("stock_agent.summary_viewer._ReviewServer.handle_request", side_effect=KeyboardInterrupt), \
             patch("webbrowser.open", side_effect=lambda url, **kwargs: urls.append(url) or True), contextlib.redirect_stdout(output):
            main()
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith(servers[0].origin + "/#"))
        self.assertEqual(servers[0].fileno(), -1)
        self.assertTrue(servers[0].access.closed)
        self.assertNotIn(urls[0].split("#")[1], output.getvalue())
        self.assertIn("read-only", output.getvalue())

    def test_missing_database_and_unsubmitted_task_are_not_initialized(self):
        missing = self.root / "absent" / "tasks.sqlite3"
        self.config["task_runtime"]["db_path"] = str(missing)
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaises(SummaryViewerError):
            self.access()
        self.assertFalse(missing.parent.exists())
        self.config["task_runtime"]["db_path"] = str(self.db)
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        task, recorded = self.store.create_task(title="Another fixture", input_payload=self.spec,
                                               case_id="different-unsubmitted-case", execution_mode="replay")
        self.assertEqual(recorded, "RECORDED")
        self.assertNotEqual(task["task_id"], self.task_id)
        with self.assertRaises(SummaryViewerError):
            self.access(task_id=task["task_id"], expected_version=task["version"])


if __name__ == "__main__":
    unittest.main()
