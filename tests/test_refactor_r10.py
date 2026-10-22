"""R10: personal identity — sessions, actor-from-session, fail-closed writes.

O-layer coverage of A46–A50:
- A46: with sessions attached, a form-forged actor is REJECTED (not merged);
  the recorded actor always comes from the server-side session.
- A47: automated principals (service tokens, bots, dify: credentials) can
  never hold a personal session — login itself refuses them.
- A48: logout/expiry take effect on the NEXT protected action.
- A49 (O half): a forged Host header is rejected by the board's loopback gate
  (the C/B proxy half stays with container acceptance).
- A50: a personal acknowledgement records its scope note and changes no
  institutional approval field.
Rollback: WITHOUT a session store the legacy form-actor path stays available
(the documented Windows-bridge rollback); WITH a store and no session,
protected writes fail closed.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.identity_providers import (
    PersonalSessionError,
    PersonalSessionStore,
    SESSION_COOKIE_NAME,
    authenticate_with_provider,
    available_providers,
)
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


def _replay_response():
    return {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": "结论", "key_findings": [], "unknowns": [],
        "evidence_refs": [], "human_questions": [
            {"question": "是否接受？", "why_needed": "确认", "options": []}],
        "model_self_assessed_confidence": 0.3}


class SessionStoreTests(unittest.TestCase):
    def test_a47_automated_principals_cannot_log_in(self):
        store = PersonalSessionStore()
        for actor in ("dify:cred-1", "service_backup", "bot-relay",
                      "stock_agent_worker", "system cron", "pi_agent-1"):
            with self.subTest(actor=actor):
                with self.assertRaises(PersonalSessionError) as caught:
                    store.login(actor=actor, provider="windows_local", subject="S-1-5-21")
                self.assertIn("automated_principal", str(caught.exception))

    def test_a48_logout_and_expiry_fail_next_action(self):
        store = PersonalSessionStore(ttl_seconds=60)
        session = store.login(actor="lead-analyst", provider="windows_local",
                              subject="S-1-5-21-x")
        self.assertEqual(store.resolve(session.session_id).actor, "lead-analyst")
        store.logout(session.session_id)
        with self.assertRaises(PersonalSessionError) as caught:
            store.resolve(session.session_id)
        self.assertIn("revoked_session", str(caught.exception))
        # Expiry: a session past its TTL fails on the NEXT resolve.
        moment = datetime.now(timezone.utc)
        expiring = store.login(actor="lead-analyst", provider="windows_local",
                               subject="S-1-y", now=moment)
        with self.assertRaises(PersonalSessionError) as caught:
            store.resolve(expiring.session_id,
                          now=moment + timedelta(seconds=3600))
        self.assertIn("expired_session", str(caught.exception))
        with self.assertRaises(PersonalSessionError) as caught:
            store.resolve("ps_nonexistent")
        self.assertIn("unknown_session", str(caught.exception))

    def test_revoke_all_for_actor(self):
        store = PersonalSessionStore()
        store.login(actor="a-lead", provider="p", subject="s1")
        store.login(actor="a-lead", provider="p", subject="s1")
        other = store.login(actor="b-lead", provider="p", subject="s2")
        self.assertEqual(store.revoke_all_for_actor("a-lead"), 2)
        self.assertEqual(store.active_count(), 1)
        self.assertEqual(store.resolve(other.session_id).actor, "b-lead")

    def test_provider_registry_fails_closed(self):
        self.assertIn("windows_local", available_providers())
        with self.assertRaises(Exception):
            authenticate_with_provider("no_such_provider", {}, "x", ["600519"])


class BoardSessionModeTestCase(unittest.TestCase):
    """Full HTTP flow against the real board server with sessions attached."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        spec = build_task_spec(research_question="R10 个人签收",
                               symbols=["600519"], execution_mode="replay",
                               config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = {}
        spec["replay_response"] = _replay_response()
        task, _ = self.store.create_task(title="R10", input_payload=spec,
                                         execution_mode="replay")
        claimed = self.store.claim_next("w-r10")
        self.store.mark_running(task["task_id"], worker_id="w-r10",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.awaiting = self.store.request_awaiting_human(
            task["task_id"], worker_id="w-r10",
            fencing_token=claimed["fencing_token"],
            human_request={"question": "是否接受？"})
        self.task_id = task["task_id"]

        # Fake provider: accepts exactly one (actor, symbols) pair.
        def authenticator(config, actor, symbols):
            if actor != "lead-analyst" or "600519" not in symbols:
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-5-21-fixture",
                    "binding_evidence_ref": "control:fixture",
                    "policy_sha256": "f" * 64}

        self.session_store = PersonalSessionStore(ttl_seconds=600)
        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=self.session_store, login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _request(self, method, path, body=None, *, cookie=None, host=None,
                 csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            if csrf:
                headers["X-Task-Board-CSRF"] = str(self.server.csrf_token)
            payload = json.dumps(body) if body is not None else None
            if host is None:
                conn.request(method, path, payload, headers)
            else:
                conn.putrequest(method, path, skip_host=True)
                for key, value in headers.items():
                    conn.putheader(key, value)
                conn.putheader("Host", host)
                conn.endheaders(payload.encode("utf-8") if payload else None)
            response = conn.getresponse()
            data = response.read().decode("utf-8")
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = {"raw": data}
            return response.status, parsed, response.getheader("Set-Cookie")
        finally:
            conn.close()

    def _login(self):
        status, body, set_cookie = self._request(
            "POST", "/api/v1/personal/login",
            {"actor": "lead-analyst", "symbols": ["600519"]})
        self.assertEqual(status, 200, body)
        cookie = set_cookie.split(";")[0]
        return cookie

    def _ack(self, cookie=None, *, with_actor_field=False, decision="acknowledge"):
        body = {"task_id": self.task_id, "decision": decision,
                "expected_version": self.awaiting["version"], "notes": "个人确认"}
        if with_actor_field:
            body["actor"] = "forged-actor"
        return self._request("POST", "/api/v1/human-responses", body,
                             cookie=cookie)

    def test_a46_forged_actor_rejected_and_session_actor_used(self):
        cookie = self._login()
        # A form actor field is REJECTED outright — never merged (A46).
        status, body, _ = self._ack(cookie=cookie, with_actor_field=True)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "actor_from_session_only")
        # Without the field, the actor comes from the SESSION.
        status, body, _ = self._ack(cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["task"]["status"], "succeeded")
        acknowledgement = body["task"]["output"]["personal_acknowledgement"]
        self.assertEqual(acknowledgement["actor"], "lead-analyst",
                         "the recorded principal is the session's, not a form value")

    def test_fail_closed_without_session(self):
        status, body, _ = self._ack(cookie=None)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "session_required")
        # A cookie for a session that never existed fails the same way.
        status, body, _ = self._ack(cookie="{0}=ps_unknown".format(SESSION_COOKIE_NAME))
        self.assertEqual(status, 403)

    def test_a48_logout_blocks_next_action(self):
        cookie = self._login()
        status, body, _ = self._request("POST", "/api/v1/personal/logout",
                                        {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body["revoked"])
        status, body, _ = self._ack(cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "session_required")

    def test_a47_login_rejects_automated_actor_via_provider(self):
        # The provider itself would reject; the session layer ALSO refuses
        # automated prefixes even if a provider wrongly accepted one.
        status, body, _ = self._request(
            "POST", "/api/v1/personal/login",
            {"actor": "dify:cred-9", "symbols": ["600519"]})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "login_failed")

    def test_a50_personal_acknowledgement_changes_no_institutional_state(self):
        cookie = self._login()
        status, body, _ = self._ack(cookie=cookie)
        self.assertEqual(status, 200)
        output = body["task"]["output"]
        self.assertEqual(output["personal_acknowledgement"]["scope"],
                         "personal_research_acknowledgement_not_institutional_approval")
        # No institutional approval key is created or altered by the action.
        for forbidden in ("institutional_approval", "quality_approval",
                          "compliance_release", "publish_approval"):
            self.assertNotIn(forbidden, output)
        self.assertIn("scope_note", body)
        self.assertIn("not institutional approval", body["scope_note"])

    def test_a49_forged_host_rejected(self):
        cookie = self._login()
        status, body, _ = self._request(
            "POST", "/api/v1/human-responses",
            {"task_id": self.task_id, "decision": "acknowledge",
             "expected_version": self.awaiting["version"]},
            cookie=cookie, host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "loopback only")


class LegacyRollbackTests(unittest.TestCase):
    def test_without_session_store_the_legacy_path_stays_available(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with project_path_override(lambda value: root / value):
            config = vault_config(root, {})
            store = TaskStore(root / "tasks.sqlite3")
            self.addCleanup(store.close)
            server = create_task_board_server(store, host="127.0.0.1", port=0,
                                              config=config)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                body = json.dumps({"task_id": "t", "decision": "reject",
                                   "actor": "legacy-user", "expected_version": 1})
                conn.request("POST", "/api/v1/human-responses", body, {
                    "Content-Type": "application/json",
                    "X-Task-Board-CSRF": str(server.csrf_token)})
                response = conn.getresponse()
                # 400 (unknown task) proves the request REACHED the store —
                # i.e. the legacy form path is still wired (rollback intact).
                self.assertEqual(response.status, 400)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
