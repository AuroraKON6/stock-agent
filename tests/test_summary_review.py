"""Synthetic Windows identity bindings and genuine TEMP vault/task producers."""
import contextlib
import copy
from datetime import timedelta
import http.client
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from stock_agent.evidence_excerpts import create_excerpt, excerpt_selector
from stock_agent.local_identity import LocalIdentityError, current_windows_subject
from stock_agent.summary_review import SummaryReviewError, review_draft, review_state, submit_draft, _now
from stock_agent.task_runtime import TaskStore, TaskVersionConflictError, TaskIdempotencyConflictError
from stock_agent.task_board import _board_state, _render_html, create_task_board_server

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document
from _summary_fixture import SummaryFixture, AUTHOR, REVIEWER, SID_A, SID_R

class SummaryReviewTests(SummaryFixture, unittest.TestCase):

    def test_submit_review_preserves_task_state_and_external_boundaries(self):
        before = self.version()
        draft = self.submit()
        self.assertEqual(draft["status"], "pending")
        self.assertEqual(self.version(), before + 1)
        supported = self.decide(draft)
        self.assertEqual(supported["status"], "supported")
        self.assertFalse(supported["external_eligible"])
        self.assertFalse(supported["institutional_approval"])
        self.assertEqual(self.store.get_task(self.task_id)["status"], "awaiting_human")
        full = review_state(self.store, self.task_id, config=self.config, include_local_content=True)
        self.assertEqual(full["local_excerpts"][0]["text"], "Revenue decreased.")
        self.assertEqual(full["review_history"][0]["summary_id"], draft["summary_id"])
        events = self.store.events(self.task_id)
        self.assertIn("summary_review_recorded", [event["event_type"] for event in events])

    def test_wrong_os_account_cannot_claim_roster_actor(self):
        count = len(self.store.events(self.task_id))
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R):
            with self.assertRaises(LocalIdentityError):
                submit_draft(self.store, self.task_id, config=self.config, actor=AUTHOR, selectors=[self.selector],
                             text="Draft", kind="human_judgment", expected_version=self.version(), command_id="wrong")
        self.assertEqual(len(self.store.events(self.task_id)), count)

    def test_enforcement_cannot_be_disabled_to_submit(self):
        self.config["research_operations"]["enforce_roster"] = False
        with self.assertRaises(LocalIdentityError):
            self.submit()

    def test_service_inactive_missing_role_and_scope_are_denied(self):
        original = copy.deepcopy(self.roster)
        for key, value in (("principal_type", "service"), ("active", False), ("active", "true"),
                           ("roles", []), ("information_side", "private"), ("coverage_entities", ["000001"]),
                           ("restricted_entities", ["600519"]), ("identity", [])):
            with self.subTest(field=key):
                self.roster = copy.deepcopy(original)
                self.roster["principals"][0][key] = value
                self.save_roster()
                with self.assertRaises(LocalIdentityError):
                    self.submit()

    def test_same_actor_and_alias_sid_cannot_self_review(self):
        draft = self.submit()
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_A):
            with self.assertRaisesRegex(SummaryReviewError, "independent"):
                review_draft(self.store, self.task_id, config=self.config, actor=AUTHOR, draft_id=draft["draft_id"],
                             summary_id=draft["summary_id"], decision="supported", notes="Self", expected_version=self.version(), command_id="self")
        self.roster["principals"][1]["identity"]["subject"] = SID_A
        self.save_roster()
        with self.assertRaises(LocalIdentityError):
            self.decide(draft)

    def test_explicit_review_authorization_required(self):
        draft = self.submit()
        self.roster["principals"][1]["authorizations"] = []
        self.save_roster()
        with self.assertRaises(LocalIdentityError):
            self.decide(draft)

    def test_modified_model_text_cannot_keep_model_provenance(self):
        with self.assertRaises(SummaryReviewError):
            self.submit(text="Rewritten by operator")
        draft = self.submit(text="Rewritten by operator", kind="human_judgment")
        self.assertEqual(draft["status"], "pending")

    def test_exact_summary_id_and_expected_task_version_required(self):
        old = self.version()
        draft = self.submit()
        with self.assertRaises(TaskVersionConflictError):
            self.decide(draft, expected_version=old)
        with self.assertRaises(SummaryReviewError):
            self.decide(draft, summary_id="different-version")
        with self.assertRaises(SummaryReviewError):
            self.decide(draft, draft_id="")

    def test_idempotency_retries_but_not_content_or_operation_reuse(self):
        initial = self.version()
        draft = self.submit(expected_version=initial)
        count = len(self.store.events(self.task_id))
        self.assertEqual(self.submit(expected_version=initial)["draft_id"], draft["draft_id"])
        self.assertEqual(len(self.store.events(self.task_id)), count)
        with self.assertRaises(TaskIdempotencyConflictError):
            self.submit(text="Changed", kind="human_judgment", expected_version=initial)
        with self.assertRaises(TaskIdempotencyConflictError):
            self.decide(draft, command_id="submit-1")
        version = self.version()
        self.decide(draft, expected_version=version)
        count = len(self.store.events(self.task_id))
        self.assertEqual(self.decide(draft, expected_version=version)["status"], "supported")
        self.assertEqual(len(self.store.events(self.task_id)), count)

    def test_legacy_feedback_cannot_reuse_summary_receipt_or_change_command(self):
        self.submit()
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.record_human_response(self.task_id, actor="fixture", decision="acknowledge", command_id="submit-1",
                                            expected_version=self.version())
        version = self.version()
        self.store.record_human_response(self.task_id, actor="fixture", decision="acknowledge", command_id="feedback-1", expected_version=version)
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.record_human_response(self.task_id, actor="fixture", decision="reject", command_id="feedback-1", expected_version=version)

    def test_two_connections_cannot_review_a_stale_version(self):
        draft = self.submit()
        old = self.version()
        second = TaskStore(self.db)
        try:
            self.decide(draft)
            with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R):
                with self.assertRaises(TaskVersionConflictError):
                    review_draft(second, self.task_id, config=self.config, actor=REVIEWER, draft_id=draft["draft_id"],
                                 summary_id=draft["summary_id"], decision="reject", notes="Stale concurrent request",
                                 expected_version=old, command_id="another-connection")
        finally:
            second.close()

    def test_read_only_pre_upgrade_database_is_not_migrated(self):
        self.store._conn.execute("DROP TABLE task_summary_reviews")
        self.store._conn.execute("DROP TABLE task_summary_drafts")
        reader = TaskStore(self.db, read_only=True)
        try:
            self.assertEqual(review_state(reader, self.task_id, config=self.config)["status"], "not_submitted")
            self.assertIsNone(reader._conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_summary_drafts'").fetchone())
        finally:
            reader.close()
        migrated = TaskStore(self.db)
        try:
            self.assertEqual(migrated.get_task(self.task_id)["status"], "awaiting_human")
            self.assertIsNotNone(migrated._conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_summary_drafts'").fetchone())
        finally:
            migrated.close()

    def test_outbox_failure_rolls_back_draft_and_version_and_receipt(self):
        old = self.version()
        with patch.object(self.store, "_emit", side_effect=RuntimeError("synthetic storage failure")):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertEqual(self.version(), old)
        self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "not_submitted")
        self.assertEqual(self.submit()["status"], "pending")

    def test_new_revision_supersedes_without_deleting_history(self):
        old = self.submit()
        self.decide(old)
        new = self.submit(command_id="submit-2", text="New human judgment", kind="human_judgment")
        state = review_state(self.store, self.task_id, config=self.config, draft_id=old["draft_id"], include_local_content=True)
        self.assertEqual(state["status"], "superseded")
        self.assertEqual(len(state["review_history"]), 1)
        self.assertEqual(new["status"], "pending")
        self.assertEqual(new["revision"], old["revision"] + 1)

    def test_request_changes_reject_and_revoke_do_not_resume_or_publish_task(self):
        first = self.submit()
        self.assertEqual(self.decide(first, decision="request_changes")["status"], "changes_requested")
        second = self.submit(command_id="submit-2")
        self.assertEqual(self.decide(second, decision="reject", command_id="reject-2")["status"], "rejected")
        third = self.submit(command_id="submit-3")
        self.decide(third, command_id="support-3")
        self.assertEqual(self.decide(third, decision="revoke", command_id="revoke-3")["status"], "revoked")
        self.assertEqual(self.store.get_task(self.task_id)["status"], "awaiting_human")

    def test_expiry_revocation_and_source_tamper_invalidate_support(self):
        draft = self.submit()
        self.decide(draft)
        with patch("stock_agent.summary_review._now", return_value=_now() + timedelta(hours=25)):
            self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "expired")
        original = copy.deepcopy(self.roster)
        self.roster["principals"][1]["active"] = False
        self.save_roster()
        self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "invalidated")
        self.roster = original
        self.save_roster()
        obj = self.root / "vault/objects" / self.sha[:2] / self.sha
        obj.write_bytes(b"changed retained object")
        self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "invalidated")

    def test_task_input_revision_invalidates_and_generic_ack_does_not_approve(self):
        draft = self.submit()
        self.store.record_human_response(self.task_id, actor="unverified-form-actor", decision="acknowledge",
                                        expected_version=self.version(), command_id="generic-ack")
        self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "pending")
        self.decide(draft)
        self.store._conn.execute("UPDATE research_tasks SET input_version=input_version+1 WHERE task_id=?", (self.task_id,))
        self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "invalidated")

    def test_unrelated_excerpt_is_not_accepted_by_task(self):
        other, _ = import_document(self.config | {"research_operations": self.config["research_operations"] | {"enforce_roster": False}},
                                   self.root, content="Other unrelated body", title="Other", signal_ids=["other-signal"])
        selector = excerpt_selector(create_excerpt(self.config, other, 0, 5))
        with self.assertRaises(SummaryReviewError):
            self.submit(selectors=[selector])

    def test_ro_status_does_not_mutate_database_and_denies_mutations(self):
        self.submit()
        before = self.db.read_bytes()
        reader = TaskStore(self.db, read_only=True)
        try:
            self.assertEqual(review_state(reader, self.task_id, config=self.config)["status"], "pending")
            with self.assertRaises(Exception):
                reader._begin()
        finally:
            reader.close()
        self.assertEqual(before, self.db.read_bytes())

    def test_cli_real_dispatch_submit_show_review_with_synthetic_identity(self):
        from stock_agent.cli import main
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        spec = self.root / "summary.json"
        spec.write_text(json.dumps({"text": "Synthetic research draft", "content_kind": "model_inference", "selectors": [self.selector]}), encoding="utf-8")
        def command(sid, *args):
            output = io.StringIO()
            with patch.object(sys, "argv", ["stock_agent", "task-summary", "--config", str(config_path), "--task-id", self.task_id, *args]), \
                 patch("stock_agent.local_identity.current_windows_subject", return_value=sid), contextlib.redirect_stdout(output):
                main()
            return json.loads(output.getvalue())
        draft = command(SID_A, "--action", "submit", "--actor", AUTHOR, "--spec", str(spec),
                        "--command-id", "cli-submit", "--expected-version", str(self.version()))
        shown = command(SID_R, "--action", "show")
        self.assertTrue(shown["local_excerpts"])
        supported = command(SID_R, "--action", "review", "--actor", REVIEWER, "--decision", "supported", "--notes", "Synthetic rationale",
                            "--draft-id", draft["draft_id"], "--summary-id", draft["summary_id"], "--expected-version", str(self.version()),
                            "--command-id", "cli-review")
        self.assertEqual(supported["status"], "supported")

    def test_board_projection_shows_state_not_originals_or_identity(self):
        self.submit()
        state = _board_state(self.store, self.config)
        projection = state["tasks"][0]["summary_review"]
        rendered = json.dumps(projection)
        self.assertNotIn(SID_A, rendered)
        self.assertNotIn("Revenue decreased", rendered)
        self.assertEqual(projection["status"], "pending")
        html = _render_html(state, "synthetic-csrf")
        self.assertIn("Summary: pending", html)
        self.assertNotIn("name=\"summary-decision\"", html)

    def test_http_shows_review_but_cannot_accept_summary_approval(self):
        self.submit()
        server = create_task_board_server(self.store, port=0, config=self.config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        # Request-handler threads get a temporary root too, never the real checkout.
        with patch("stock_agent.config.PROJECT_ROOT", self.root):
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/api/v1/tasks")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                state = json.loads(response.read())
                self.assertEqual(state["tasks"][0]["summary_review"]["status"], "pending")
                connection.request("POST", "/api/v1/human-responses", json.dumps({"task_id": self.task_id,
                    "actor": REVIEWER, "decision": "supported", "expected_version": self.version()}),
                    {"X-Task-Board-CSRF": server.csrf_token})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                self.assertEqual(review_state(self.store, self.task_id, config=self.config)["status"], "pending")
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    @unittest.skipUnless(os.name == "nt", "read-only Windows token probe")
    def test_native_token_probe_is_read_only_and_no_account_is_enrolled(self):
        before = Path(self.config["research_operations"]["roster_path"]).read_bytes()
        subject = current_windows_subject()
        self.assertRegex(subject, r"^S-1-(?:\d+-)+\d+$")
        self.assertEqual(Path(self.config["research_operations"]["roster_path"]).read_bytes(), before)
