"""R22: bounded subtask graph — governance constraints, cancel propagation.

The Dify-role-orchestration half of R22 is NOT_RUN (depends on R17); this
batch delivers the persistent task-graph rules every orchestrator must pass
through: scope, budget, depth, fan-out, duplicates, self-parenting, cycles,
cancel propagation, partial-failure independence.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.subtask_graph import (
    DEFAULT_MAX_CHILDREN,
    SubtaskGraphError,
    cancel_subtree,
    propose_subtasks,
)
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import STATUS_CANCELLED
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


class R22GraphTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self.counter = [0]

    def parent(self, *, symbols=("600519",), terminal=False, parent_id=""):
        self.counter[0] += 1
        spec = build_task_spec(
            research_question="父任务问题 {0}".format(self.counter[0]),
            symbols=list(symbols), execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = {}
        spec["replay_response"] = {
            "schema_version": "1.0", "task_id": "placeholder",
            "summary": "s", "key_findings": [], "unknowns": [],
            "evidence_refs": [], "human_questions": [],
            "model_self_assessed_confidence": 0.3}
        task, _ = self.store.create_task(
            title="parent", input_payload=spec, execution_mode="replay",
            parent_task_id=parent_id)
        if terminal:
            claimed = self.store.claim_next("w")
            self.store.mark_running(task["task_id"], worker_id="w",
                                    fencing_token=claimed["fencing_token"],
                                    expected_version=claimed["version"])
            self.store.complete(task["task_id"], worker_id="w",
                                fencing_token=claimed["fencing_token"],
                                output={"summary": "done"}, model_call={})
        return self.store.get_task(task["task_id"])

    def propose(self, parent, proposals, **kwargs):
        return propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                proposals=proposals, **kwargs)

    def _claim_specific(self, task_id):
        """Claim in a loop until we hold task_id (claim_next picks the oldest
        queued task, which may be the parent or a sibling). Non-target claims
        stay leased while the loop moves past them, then are returned to the
        queue via a retryable failure so their side effects don't leak into
        the assertions."""
        side_claims = []
        claimed = None
        for _ in range(10):
            claim = self.store.claim_next("w")
            if claim is None:
                break
            if claim["task_id"] == task_id:
                claimed = claim
                break
            side_claims.append(claim)
        for claim in side_claims:
            self.store.fail(claim["task_id"], worker_id="w",
                            fencing_token=claim["fencing_token"],
                            error="claim probe: not the target task",
                            retryable=True)
        if claimed is None:
            raise AssertionError("never claimed {0}".format(task_id))
        return claimed


class GovernanceTests(R22GraphTestCase):
    def test_scope_widening_refused(self):
        parent = self.parent()
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(parent, [{"role_id": "event_analyst", "question": "q",
                                   "symbols": ["600519", "000001"]}])
        self.assertIn("scope_widened", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), 1,
                         "a refused fan-out creates nothing")

    def test_unknown_role_refused(self):
        parent = self.parent()
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(parent, [{"role_id": "rookie_writer", "question": "q"}])
        self.assertIn("role_not_allowed", str(caught.exception))

    def test_depth_cap_blocks_second_level(self):
        parent = self.parent()
        result = self.propose(parent, [{"role_id": "event_analyst", "question": "c1"}])
        child = self.store.get_task(result["created"][0]["task_id"])
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(child, [{"role_id": "counter_evidence", "question": "gc"}])
        self.assertIn("depth_exceeded", str(caught.exception))

    def test_fanout_cap_and_duplicate_detection(self):
        parent = self.parent()
        first = self.propose(parent, [
            {"role_id": "event_analyst", "question": "分析事件"},
            {"role_id": "counter_evidence", "question": "提出反证"}])
        self.assertEqual(len(first["created"]), 2)
        again = self.propose(parent, [
            {"role_id": "event_analyst", "question": "分析事件!"},  # dup by identity
            {"role_id": "synthesis_drafter", "question": "综合"}])
        self.assertEqual([d["reason"] for d in again["duplicates"]],
                         ["duplicate_child"])
        self.assertEqual(len(again["created"]), 1)
        # Now at cap 3: any further fan-out is refused whole.
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(parent, [{"role_id": "quality_check", "question": "查"}])
        self.assertIn("fanout_exceeded", str(caught.exception))

    def test_budget_shared_across_children(self):
        parent = self.parent()
        result = self.propose(parent, [
            {"role_id": "event_analyst", "question": "a", "budget_reserve": 2},
            {"role_id": "counter_evidence", "question": "b", "budget_reserve": 1}],
            budget_limit=4)
        self.assertEqual(len(result["created"]), 2)
        # Existing outstanding 3 + requested 2 > 4 → whole batch refused.
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(parent, [
                {"role_id": "synthesis_drafter", "question": "s", "budget_reserve": 2}],
                budget_limit=4)
        self.assertIn("budget_exceeded", str(caught.exception))

    def test_terminal_parent_refuses_self_parenting_refuses(self):
        done = self.parent(terminal=True)
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(done, [{"role_id": "event_analyst", "question": "x"}])
        self.assertIn("parent_terminal", str(caught.exception))
        parent = self.parent()
        result = self.propose(parent, [{"role_id": "event_analyst", "question": "x"}])
        self.assertEqual(len(result["created"]), 1,
                         "sanity: a normal proposal on a fresh parent passes")
        # self-parenting is rejected explicitly at the parent lookup boundary
        rogue = self.parent()
        self.store._conn.execute(
            "UPDATE research_tasks SET parent_task_id = ? WHERE task_id = ?",
            (rogue["task_id"], rogue["task_id"]))
        self.store._conn.commit()
        with self.assertRaises(SubtaskGraphError) as caught:
            self.propose(self.store.get_task(rogue["task_id"]),
                         [{"role_id": "event_analyst", "question": "y"}])
        self.assertIn("self_parented", str(caught.exception))


class CancelPropagationTests(R22GraphTestCase):
    def test_cancel_parent_propagates_to_descendants(self):
        parent = self.parent()
        result = self.propose(parent, [
            {"role_id": "event_analyst", "question": "one"},
            {"role_id": "counter_evidence", "question": "two"}])
        child_ids = [c["task_id"] for c in result["created"]]
        report = cancel_subtree(self.store, parent["task_id"], actor="operator",
                                reason="用户取消整棵树")
        self.assertEqual(report["count"], 3,
                         "parent + both non-terminal descendants cancelled")
        for task_id in [parent["task_id"], *child_ids]:
            self.assertEqual(self.store.get_task(task_id)["status"], STATUS_CANCELLED)
        # Each cancellation left its own audit event.
        for task_id in child_ids:
            events = [e["event_type"] for e in self.store.events(task_id)]
            self.assertIn("task_cancelled", events)

    def test_terminal_descendant_history_stands(self):
        parent = self.parent()
        done_child = self.propose(parent, [
            {"role_id": "event_analyst", "question": "finished"}])
        done_id = done_child["created"][0]["task_id"]
        claimed = self._claim_specific(done_id)
        self.store.mark_running(done_id, worker_id="w",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.complete(done_id, worker_id="w",
                            fencing_token=claimed["fencing_token"],
                            output={"summary": "ok"}, model_call={})
        pending_child = self.propose(parent, [
            {"role_id": "counter_evidence", "question": "pending"}])
        report = cancel_subtree(self.store, parent["task_id"], actor="op", reason="r")
        cancelled_ids = {entry["task_id"] for entry in report["cancelled"]}
        self.assertIn(parent["task_id"], cancelled_ids)
        self.assertIn(pending_child["created"][0]["task_id"], cancelled_ids)
        self.assertNotIn(done_id, cancelled_ids,
                         "a terminal descendant keeps its history")
        self.assertEqual(self.store.get_task(done_id)["status"], "succeeded")


class PartialFailureTests(R22GraphTestCase):
    def test_one_child_failing_leaves_siblings_untouched(self):
        parent = self.parent()
        result = self.propose(parent, [
            {"role_id": "event_analyst", "question": "will fail"},
            {"role_id": "counter_evidence", "question": "will wait"}])
        fail_id, wait_id = (result["created"][0]["task_id"],
                            result["created"][1]["task_id"])
        # Child A: claim and terminally fail (attempts exhausted).
        claimed = self._claim_specific(fail_id)
        self.store.mark_running(fail_id, worker_id="w",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.fail(fail_id, worker_id="w", fencing_token=claimed["fencing_token"],
                        error="model unavailable", retryable=False)
        self.assertEqual(self.store.get_task(fail_id)["status"], "failed")
        # Sibling is untouched — still queued, independently runnable.
        self.assertEqual(self.store.get_task(wait_id)["status"], "queued")
        # And the parent keeps its own state.
        self.assertEqual(self.store.get_task(parent["task_id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
