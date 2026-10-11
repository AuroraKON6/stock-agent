from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.review_queue import (
    ReviewQueuePolicyError,
    build_review_queue,
    record_review_queue_action,
    write_review_queue_snapshot,
)


TZ = timezone(timedelta(hours=8))


def _principal(actor_id="human:lead", principal_type="human", active=True, verified=True):
    return {
        "actor_id": actor_id,
        "display_name": actor_id,
        "principal_type": principal_type,
        "active": active,
        "roles": ["lead_analyst"] if principal_type == "human" else ["information_analyst"],
        "information_side": "public",
        "qualifications": {
            "securities_analyst_registration": {
                "status": "verified" if verified else "unverified",
                "registration_id": "REG-001" if verified else "",
            }
        },
        "authorizations": [],
        "coverage_entities": ["*"],
        "restricted_entities": [],
        "max_active_cases": 10,
    }


def _pending(signal_id="sig-1", priority="P1", created_at="2026-08-13T08:30:00+08:00"):
    return {
        "signal_id": signal_id,
        "title": "Material filing",
        "priority": priority,
        "source": "exchange_filing",
        "source_type": "filing",
        "symbols": ["600000"],
        "topics": ["earnings"],
        "created_at": created_at,
        "llm_analysis": {
            "confidence": 0.8,
            "impact_direction": "positive",
            "key_thesis": "Needs human verification",
            "review_questions": ["Is the filing primary-source evidence?"],
        },
    }


class ReviewQueueV23Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        for relative in ("config", "data/review"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        self.config = {
            "review_queue": {
                "enabled": True,
                "enforce_roster": True,
                "assignment_mode": "suggest_only",
                "sla_hours": {"P0": 0.5, "P1": 2, "P2": 8, "P3": 24},
            },
            "research_operations": {
                "enabled": True,
                "enforce_roster": True,
                "roster_path": "config/roster.json",
                "restrictions_path": "config/restrictions.json",
                "business_calendar": {
                    "weekdays": [0, 1, 2, 3, 4],
                    "workday_start": "08:30",
                    "workday_end": "18:00",
                    "holidays": [],
                },
            },
        }
        self._write_roster([])
        (self.root / "config/restrictions.json").write_text(
            json.dumps({"entries": []}), encoding="utf-8"
        )
        self.paths = project_path_override(lambda value: self.root / value)
        self.paths.__enter__()

    def tearDown(self):
        self.paths.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _write_roster(self, principals):
        (self.root / "config/roster.json").write_text(
            json.dumps({"principals": principals}), encoding="utf-8"
        )

    def _write_jsonl(self, relative, rows):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def test_pending_rows_are_deduplicated_into_one_queue_item_per_signal(self):
        self._write_jsonl(
            "data/review/pending_review_a.jsonl",
            [_pending("sig-1"), _pending("sig-1"), _pending("sig-2", "P2")],
        )
        queue = build_review_queue(self.config, now=datetime(2026, 8, 13, 9, tzinfo=TZ))
        self.assertEqual(3, queue["summary"]["pending_rows"])
        self.assertEqual(2, queue["summary"]["pending_unique"])
        self.assertEqual(1, queue["summary"]["pending_duplicate_rows"])
        self.assertEqual(2, queue["summary"]["outstanding_count"])

    def test_configured_review_dir_does_not_read_default_queue(self):
        self._write_jsonl("data/review/pending_review_default.jsonl", [_pending("prod")])
        isolated = json.loads(json.dumps(self.config))
        isolated["review_queue"]["review_dir"] = "data/uat/review"
        self._write_jsonl("data/uat/review/pending_review_uat.jsonl", [_pending("uat")])

        queue = build_review_queue(isolated, now=datetime(2026, 8, 13, 9, tzinfo=TZ))

        self.assertEqual(["uat"], [item["signal_id"] for item in queue["items"]])
        self.assertEqual(1, queue["summary"]["pending_rows"])

    def test_completed_record_closes_the_matching_item(self):
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        completed = {**_pending(), "reviewed_at": "2026-08-13T09:15:00+08:00", "was_effective": "true"}
        self._write_jsonl("data/review/completed_review_a.jsonl", [completed])
        queue = build_review_queue(self.config, now=datetime(2026, 8, 13, 10, tzinfo=TZ))
        self.assertEqual("completed", queue["items"][0]["status"])
        self.assertEqual(0, queue["summary"]["outstanding_count"])
        self.assertEqual(1, queue["summary"]["completed_count"])

    def test_business_sla_distinguishes_on_track_at_risk_and_overdue(self):
        rows = [
            _pending("sig-p0", "P0", "2026-08-13T08:30:00+08:00"),
            _pending("sig-p1", "P1", "2026-08-13T08:30:00+08:00"),
            _pending("sig-p2", "P2", "2026-08-13T08:30:00+08:00"),
        ]
        self._write_jsonl("data/review/pending_review_a.jsonl", rows)
        queue = build_review_queue(self.config, now=datetime(2026, 8, 13, 10, 15, tzinfo=TZ))
        by_id = {item["signal_id"]: item for item in queue["items"]}
        self.assertEqual("overdue", by_id["sig-p0"]["sla_status"])
        self.assertEqual("at_risk", by_id["sig-p1"]["sla_status"])
        self.assertEqual("on_track", by_id["sig-p2"]["sla_status"])
        self.assertEqual("internal_operating_policy_not_regulatory_deadline", queue["sla_policy_type"])

    def test_missing_human_roster_never_invents_assignment(self):
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        item = build_review_queue(self.config)["items"][0]
        self.assertEqual("", item["assignment_recommendation"])
        self.assertEqual([], item["eligible_human_reviewers"])
        self.assertFalse(item["automatic_assignment_executed"])

    def test_verified_human_is_only_recommended_not_assigned(self):
        self._write_roster([_principal()])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        item = build_review_queue(self.config)["items"][0]
        self.assertEqual("human:lead", item["assignment_recommendation"])
        self.assertEqual("", item["assigned_to"])
        self.assertFalse(item["automatic_assignment_executed"])

    def test_service_and_unknown_actors_cannot_change_queue_state(self):
        self._write_roster([_principal("service:source", "service")])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        with self.assertRaises(ReviewQueuePolicyError):
            record_review_queue_action(
                self.config, "sig-1", "start_review", "service:source", "lead_analyst"
            )
        with self.assertRaises(ReviewQueuePolicyError):
            record_review_queue_action(
                self.config, "sig-1", "start_review", "human:unknown", "lead_analyst"
            )

    def test_unverified_human_cannot_change_queue_state(self):
        self._write_roster([_principal(verified=False)])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        with self.assertRaises(ReviewQueuePolicyError):
            record_review_queue_action(
                self.config, "sig-1", "start_review", "human:lead", "lead_analyst"
            )

    def test_valid_human_start_and_complete_are_append_only(self):
        self._write_roster([_principal()])
        original = self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        original_bytes = original.read_bytes()
        started = record_review_queue_action(
            self.config, "sig-1", "start_review", "human:lead", "lead_analyst"
        )
        self.assertEqual("in_review", started["review_queue"]["items"][0]["status"])
        completed = record_review_queue_action(
            self.config,
            "sig-1",
            "complete_review",
            "human:lead",
            "lead_analyst",
            fields={"decision_taken": "confirmed", "was_effective": "true"},
        )
        self.assertEqual("completed", completed["review_queue"]["items"][0]["status"])
        self.assertTrue(Path(started["record_path"]).exists())
        self.assertTrue(Path(completed["record_path"]).exists())
        self.assertEqual(original_bytes, original.read_bytes())
        self.assertEqual(1, len(list((self.root / "data/review/queue_actions").glob("*.jsonl"))))
        self.assertEqual(1, len(list((self.root / "data/review").glob("completed_review_*.jsonl"))))

    def test_invalid_transition_and_incomplete_completion_are_rejected(self):
        self._write_roster([_principal()])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        with self.assertRaises(ReviewQueuePolicyError):
            record_review_queue_action(
                self.config, "sig-1", "complete_review", "human:lead", "lead_analyst"
            )
        record_review_queue_action(
            self.config, "sig-1", "start_review", "human:lead", "lead_analyst"
        )
        with self.assertRaises(ReviewQueuePolicyError):
            record_review_queue_action(
                self.config,
                "sig-1",
                "complete_review",
                "human:lead",
                "lead_analyst",
                fields={"decision_taken": "", "was_effective": "true"},
            )

    def test_snapshot_writes_index_timestamped_and_latest_outputs(self):
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        result = write_review_queue_snapshot(
            self.config, now=datetime(2026, 8, 13, 9, tzinfo=TZ)
        )
        for key in (
            "review_queue_index_path",
            "review_queue_path",
            "review_queue_json_path",
            "review_queue_latest_path",
            "review_queue_latest_json_path",
        ):
            self.assertTrue(Path(result[key]).exists(), key)
        latest = json.loads(Path(result["review_queue_latest_json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(1, latest["summary"]["outstanding_count"])

    def test_review_workbench_renders_canonical_queue_without_prefilled_actor(self):
        from stock_agent.review_workbench import render_review_queue_workbench_html

        self._write_roster([_principal()])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        html = render_review_queue_workbench_html(
            signal_id="sig-1", config=self.config, csrf_token="csrf-test"
        )
        self.assertIn("Signal Review", html)
        self.assertIn("Human Signal Review Queue", html)
        self.assertIn("start_review", html)
        self.assertIn('name="actor" required autocomplete="off"', html)
        self.assertNotIn('name="actor" value="human:lead"', html)
        self.assertIn("automatic assignment", html)

    def test_review_workbench_places_detail_before_paginated_queue(self):
        from stock_agent.review_workbench import load_review_queue_workbench_state, render_review_queue_workbench_html

        self._write_roster([_principal()])
        self._write_jsonl(
            "data/review/pending_review_many.jsonl",
            [_pending(signal_id="sig-{0:02d}".format(index)) for index in range(30)],
        )
        state = load_review_queue_workbench_state(config=self.config, page=2)
        self.assertEqual(30, state["filtered_count"])
        self.assertEqual(2, state["page_count"])
        self.assertEqual(5, len(state["items"]))
        html = render_review_queue_workbench_html(
            signal_id="sig-29", config=self.config, csrf_token="csrf-test", page=2,
        )
        self.assertLess(html.index('id="review-detail"'), html.index("Queue (26-30 of 30)"))
        self.assertIn("Page 2 of 2", html)
        self.assertIn("rel=\"prev\"", html)
        self.assertNotIn("sig-00</a>", html)

    def test_review_workbench_submission_uses_same_roster_and_transition_policy(self):
        from stock_agent.review_workbench import submit_review_queue_workbench_action

        self._write_roster([_principal()])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        result = submit_review_queue_workbench_action({
            "signal_id": "sig-1",
            "action": "start_review",
            "actor": "human:lead",
            "actor_role": "lead_analyst",
        }, self.config)
        self.assertEqual("in_review", result["record"]["to_status"])
        self.assertEqual("human:lead", result["review_queue"]["items"][0]["assigned_to"])

    def test_review_workbench_rejects_unsafe_or_service_actions(self):
        from stock_agent.review_workbench import submit_review_queue_workbench_action

        self._write_roster([_principal("service:source", "service")])
        self._write_jsonl("data/review/pending_review_a.jsonl", [_pending()])
        with self.assertRaises(ValueError):
            submit_review_queue_workbench_action({
                "signal_id": "sig-1", "action": "place_order",
                "actor": "service:source", "actor_role": "information_analyst",
            }, self.config)
        with self.assertRaises(ReviewQueuePolicyError):
            submit_review_queue_workbench_action({
                "signal_id": "sig-1", "action": "start_review",
                "actor": "service:source", "actor_role": "lead_analyst",
            }, self.config)


if __name__ == "__main__":
    unittest.main()
