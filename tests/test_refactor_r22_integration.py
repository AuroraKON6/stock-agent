"""R22 (integration): limited multi-agent + personal review on real components.

Everything here runs the PRODUCTION entry points — the R22 subtask graph,
the R17 role flow, the standard worker boundary in replay mode, the R40
personal workspace and the task store's own ledgers. One frozen synthetic
event enters BOTH modes (single-role full chain and controlled
analysis → counter-evidence → synthesis), and the personal review projection
reads the result without writing anything.

Acceptance rows covered:
- cycles/boundaries: self/cycle/scope/case/depth/fan-out/dependency refused
  with zero creation; a legal proposal goes through the controlled entry.
- duplicates/budget: same-identity duplicate and concurrent submissions
  reuse the task; an exhausted parent budget adds nothing and meters nothing.
- parent cancel / partial failure: cancel propagates to non-terminal
  descendants with events, terminal children keep their output; one step
  failing leaves its siblings and the parent untouched, audit trail intact.
- sources/review: three roles + summary traced item by item; model roles,
  independent evidence and human sign-off stay distinct; a rule change is
  only a pending candidate and cannot alter the approved version; the review
  only reads what the calling identity may read.
- fallback: the new case runs the single-role chain; created subtasks,
  results and metering are retained and reconcilable in both modes.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from stock_agent import subtask_graph
from stock_agent.config import project_path_override
from stock_agent.personal_review import (
    ReviewAccessError,
    actor_kind,
    project_review,
)
from stock_agent.personal_workspace import (
    WorkspaceError,
    acknowledge_completed_research,
    cancel_personal_task,
    create_personal_research,
)
from stock_agent.research_worker import run_research_task
from stock_agent.role_execution import (
    FlowRegistry,
    RoleFlowError,
    bind_role_flow,
    define_role_flow,
    execute_role_step,
    finish_role_flow,
    prompt_artifact,
)
from stock_agent.rule_candidates import (
    RuleCandidateBook,
    RuleCandidateError,
    propose_rule_change,
)
from stock_agent.subtask_graph import (
    SubtaskGraphError,
    cancel_subtree,
    propose_subtasks,
)
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import (
    TERMINAL_STATUSES,
    TaskVersionConflictError,
)
from stock_agent.task_supply import build_task_spec
from stock_agent.workflow_service import cancel_task as workflow_cancel_task

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

OWNER = "review-owner"
OTHER = "review-other"
ROLES = ("event_analyst", "counter_evidence", "synthesis_drafter")
GROUPS = [{"roles": ["event_analyst", "counter_evidence"]},
          {"roles": ["synthesis_drafter"]}]
QUESTION = "合成事件：样例公司公告回购股份并终止一项在建项目，对本期业绩有何影响？"


def replay(summary, refs, confidence=0.4):
    return {"schema_version": "1.0", "task_id": "placeholder", "summary": summary,
            "key_findings": [], "unknowns": [], "evidence_refs": list(refs),
            "human_questions": [], "model_self_assessed_confidence": confidence}


def make_flow(synthesis_text="综合草稿提示词 v1", version=1):
    prompts = {
        "event_analyst": prompt_artifact("event_analyst", 1, "事件分析提示词 v1"),
        "counter_evidence": prompt_artifact("counter_evidence", 1, "反证提示词 v1"),
        "synthesis_drafter": prompt_artifact("synthesis_drafter", 1, synthesis_text),
    }
    return define_role_flow(flow_id="r22_event_draft_flow", version=version,
                            groups=GROUPS, prompts=prompts)


class R22IntegrationCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.config["personal_evidence"] = {"root": str(self.root / "personal_evidence")}
        self.db_path = Path(self.config["task_runtime"]["db_path"])
        self.store = TaskStore(self.db_path)
        self.addCleanup(self.store.close)
        self.doc_id, self.doc_sha = import_document(
            self.config, self.root,
            content="合成公告（冻结样本）：样例公司公告拟回购股份并终止一项在建项目。",
            title="合成公告：回购与终止在建项目", signal_ids=["sig-r22-1"])
        self.doc2_id, self.doc2_sha = import_document(
            self.config, self.root,
            content="合成补充公告（冻结样本）：同一事项的进展说明。",
            title="合成补充公告：进展说明", signal_ids=["sig-r22-2"])
        self.registry = FlowRegistry()
        self.counter = [0]

    # ---------------- drivers ---------------- #

    def spec(self, *, question=QUESTION, budget_limit=None, cites=None):
        spec = build_task_spec(research_question=question, symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = ["sig-r22-1", "sig-r22-2"]
        spec["public_context"]["evidence_summaries"] = {
            self.doc_id: {"summary": "合成公告（冻结样本）", "sha256": self.doc_sha},
            self.doc2_id: {"summary": "合成补充公告（冻结样本）", "sha256": self.doc2_sha},
        }
        if budget_limit is not None:
            spec["budget_limit"] = int(budget_limit)
        spec["replay_response"] = replay("单角色整链结论（合成回放）。",
                                         [self.doc_id] if cites is None else cites)
        return spec

    def create_root(self, *, case_id="", budget_limit=None, question=QUESTION):
        self.counter[0] += 1
        task, _ = self.store.create_task(
            title="R22 root {0}".format(self.counter[0]),
            input_payload=self.spec(question=question, budget_limit=budget_limit),
            case_id=case_id or "case-r22-{0}".format(self.counter[0]),
            execution_mode="replay", actor="operator")
        return self.store.get_task(task["task_id"])

    def run_full_chain(self, root):
        claim = self.store.claim_next("full-chain-worker", exclude_role_bound=True)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["task_id"], root["task_id"])
        result = run_research_task(self.store, claim["task_id"],
                                   worker_id="full-chain-worker", config=self.config)
        return result, self.store.get_task(root["task_id"])

    def approved_flow(self, flow=None):
        flow = flow or make_flow()
        self.registry.register(flow)
        self.registry.approve(flow, actor="human-approver")
        return flow

    def bound_root(self, *, flow=None, budget_limit=None):
        flow = self.approved_flow(flow)
        root = self.create_root(budget_limit=budget_limit)
        fresh = bind_role_flow(self.store, root["task_id"], flow=flow,
                               registry=self.registry, actor="operator")
        self.assertEqual(fresh["input_version"], 2)
        return self.store.get_task(root["task_id"]), flow

    def run_step(self, root, flow, role, summary, refs=None):
        return execute_role_step(
            self.store, root["task_id"], role_id=role, flow=flow,
            registry=self.registry, config=self.config,
            step_overrides={"replay_response": replay(
                summary, [self.doc_id] if refs is None else refs)})

    def run_role_flow(self, root, flow):
        for role, summary in zip(ROLES, ("事件分析（合成回放）。",
                                         "反证（合成回放）。",
                                         "综合草稿（合成回放）。")):
            result = self.run_step(root, flow, role, summary)
            self.assertEqual(result["status"], "succeeded", result)
        return finish_role_flow(self.store, root["task_id"], flow=flow,
                                registry=self.registry)

    def identity(self, *, actor=OWNER, cases=("*",), scopes=("read_research",)):
        return {"credential_id": "review-{0}".format(actor), "actor_id": actor,
                "scopes": list(scopes), "case_scope": list(cases)}

    def children(self, root_id):
        return [task for task in self.store.list_tasks()
                if str(task.get("parent_task_id") or "") == str(root_id)]


# ---------------------------------------------------------------------- #
# 回退 / dual mode                                                        #
# ---------------------------------------------------------------------- #

class DualModeTests(R22IntegrationCase):
    def test_same_frozen_event_dual_mode_structure_citations_and_replay_cost(self):
        chain_root = self.create_root(case_id="case-chain")
        chain_result, chain_task = self.run_full_chain(chain_root)
        self.assertEqual(chain_result["status"], "succeeded")
        self.assertEqual(chain_task["attempt_count"], 1)
        self.assertFalse(chain_task["model_call"]["called"],
                         "replay mode makes no provider call")
        chain_output = chain_task["output"]

        role_root, flow = self.bound_root()
        finish = self.run_role_flow(role_root, flow)
        role_task = self.store.get_task(role_root["task_id"])
        steps = self.children(role_task["task_id"])
        by_role = {task["role_id"]: task for task in steps}

        self.assertEqual(finish["status"], "succeeded")
        self.assertEqual(set(by_role), set(ROLES))
        self.assertEqual(set(role_task["output"]["role_outputs"]), set(ROLES))
        self.assertEqual(sum(task["attempt_count"] for task in steps), 3)
        self.assertEqual(role_task["attempt_count"], 1,
                         "the root carries only the orchestration claim; its "
                         "three role steps carry the model attempts")
        self.assertFalse(role_task["model_call"]["called"])
        self.assertEqual(len(role_task["model_call"]["steps"]), 3)

        # Same frozen event, same citation allowlist, same report schema.
        allowed = sorted(role_task["input_payload"]["public_context"]["evidence_summaries"])
        self.assertEqual(allowed, sorted(chain_task["input_payload"]
                                        ["public_context"]["evidence_summaries"]))
        for task in steps:
            self.assertEqual(sorted(task["output"]["evidence_refs"]), [self.doc_id])
            self.assertEqual(task["input_payload"]["public_context"]["evidence_summaries"],
                             role_task["input_payload"]["public_context"]["evidence_summaries"])
        self.assertEqual(sorted(chain_output["evidence_refs"]), [self.doc_id])
        self.assertEqual(sorted(by_role["synthesis_drafter"]["output"]),
                         sorted(chain_output))
        self.assertEqual(by_role["synthesis_drafter"]["input_payload"]["research_question"],
                         chain_task["input_payload"]["research_question"])

        # Bounded cost difference: 1 replay answer vs 3, itemized per step.
        self.assertEqual(sum(task["attempt_count"] for task in steps), 3)
        self.assertEqual(chain_task["attempt_count"], 1)
        self.assertEqual(chain_task["input_payload"]["budget_reserve"]
                         if "budget_reserve" in chain_task["input_payload"] else 0, 0)
        self.assertTrue(all(task["input_payload"]["origin"]["kind"] == "subtask"
                            for task in steps))

        # Both modes are visible to the review, with their own metering.
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[chain_task["task_id"], role_task["task_id"]])
        rows = {row["task_id"]: row for row in review["tasks"]}
        self.assertEqual(rows[chain_task["task_id"]]["producer"]["kind"],
                         "model_single_role")
        self.assertEqual(rows[chain_task["task_id"]]["metering"]["attempts"], 1)
        for role in ROLES:
            row = rows[by_role[role]["task_id"]]
            self.assertEqual(row["producer"]["kind"], "model_role")
            self.assertEqual(row["metering"]["attempts"], 1)
            self.assertEqual(row["sources"]["citations_outside_allowlist"], [])
        self.assertEqual(len(review["attribution"]["model_roles"]), 3)
        self.assertEqual(review["attribution"]["independent_evidence_count"], 1,
                         "the same document cited by three roles is ONE source")

    def test_fallback_keeps_subtasks_results_and_metering_reconcilable(self):
        role_root, flow = self.bound_root()
        self.run_role_flow(role_root, flow)
        before = {task["task_id"]: (task["status"], task["attempt_count"],
                                    task["output"].get("summary"))
                  for task in self.store.list_tasks()}
        # A new case after role mode falls back to the single-role chain.
        fallback = self.create_root(case_id="case-fallback")
        result, task = self.run_full_chain(fallback)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.children(fallback["task_id"]), [],
                         "the fallback case creates no subtasks")
        after = {task_["task_id"]: (task_["status"], task_["attempt_count"],
                                    (task_.get("output") or {}).get("summary"))
                 for task_ in self.store.list_tasks()}
        self.assertEqual({key: after[key] for key in before}, before,
                         "the role-mode subtasks, results and counting stand")
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[role_root["task_id"], fallback["task_id"]])
        self.assertEqual(len(review["tasks"]), 5, "1 root + 3 steps + 1 fallback root")

    def test_review_traces_relations_versions_roles_and_failures(self):
        root, flow = self.bound_root()
        self.run_role_flow(root, flow)
        root = self.store.get_task(root["task_id"])
        steps = {task["role_id"]: task for task in self.children(root["task_id"])}
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[root["task_id"]])
        rows = {row["task_id"]: row for row in review["tasks"]}
        root_row = rows[root["task_id"]]
        self.assertEqual(sorted(root_row["relations"]["children"]),
                         sorted(task["task_id"] for task in steps.values()))
        self.assertEqual([item["input_version"] for item in root_row["input"]["snapshots"]],
                         [1, 2], "the bind is an immutable new input version")
        self.assertEqual(root_row["input"]["rule"]["flow_sha256"], flow["flow_sha256"])
        for role, task in steps.items():
            row = rows[task["task_id"]]
            self.assertEqual(row["relations"]["parent_task_id"], root["task_id"])
            self.assertEqual(row["relations"]["root_task_id"], root["task_id"])
            self.assertEqual(row["input"]["step_prompt_sha256"],
                             flow["prompts"][role]["sha256"])
            self.assertEqual(row["result"]["output_sha256"],
                             hashlib.sha256(json.dumps(
                                 task["output"], ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), default=str).encode()).hexdigest())
            self.assertEqual(row["sources"]["delivered_evidence"][self.doc_id]["sha256"],
                             self.doc_sha)
        self.assertEqual(root_row["result"]["role_outputs"].keys(), set(ROLES))

    def test_attribution_separates_model_evidence_and_human(self):
        root, flow = self.bound_root()
        self.run_role_flow(root, flow)
        # A human acknowledging the aggregate result IS sign-off.
        root = self.store.get_task(root["task_id"])
        acknowledge_completed_research(self.store, actor=OWNER, task_id=root["task_id"],
                                       expected_version=root["version"], note="看过")
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[root["task_id"]])
        self.assertEqual(len(review["attribution"]["human_signoffs"]), 1)
        self.assertEqual(review["attribution"]["distinct_human_actors"], [OWNER])
        self.assertEqual(len(review["attribution"]["model_roles"]), 3)
        for entry in review["attribution"]["model_roles"]:
            self.assertNotIn(entry["role_id"], review["attribution"]["distinct_human_actors"])

        # A model-role identity claiming sign-off is NOT human sign-off.
        chain_root = self.create_root(case_id="case-claim")
        _, chain_task = self.run_full_chain(chain_root)
        self.store.record_personal_acknowledgement(
            chain_task["task_id"], actor="event_analyst", notes="模型自称签收",
            expected_version=chain_task["version"])
        review2 = project_review(self.store, identity=self.identity(),
                                 task_ids=[chain_task["task_id"]])
        row = review2["tasks"][0]
        self.assertEqual(row["signoff"]["human"], [])
        self.assertEqual(len(row["signoff"]["rejected_automated_claims"]), 1)
        self.assertFalse(row["signoff"]["acknowledged_by_human"])
        self.assertEqual(actor_kind("event_analyst"), "automated")
        self.assertEqual(actor_kind("role-orchestrator"), "automated")
        self.assertEqual(actor_kind("role-orchestrator:synthetic-credential"),
                         "automated",
                         "the role API's credential suffix must not launder an orchestrator")
        self.assertEqual(actor_kind(OWNER), "human")

    def test_prepared_context_without_execution_is_not_delivered(self):
        spec = self.spec()
        spec.pop("replay_response")
        self.counter[0] += 1
        task, _ = self.store.create_task(
            title="Prepared context without an answer", input_payload=spec,
            case_id="case-prepared", execution_mode="replay", actor="operator")
        result, row = self.run_full_chain(task)
        self.assertEqual(row["status"], "queued",
                         "the missing replay answer requeues within the budget")
        self.assertTrue(row["model_call"].get("error"))
        self.assertFalse(row["model_call"].get("replay_source"))
        self.assertFalse(row["model_call"].get("called"))
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[task["task_id"]])
        sources = review["tasks"][0]["sources"]
        self.assertEqual(sources["delivered_evidence"], {},
                         "a prepared outbound context is not a delivery")
        self.assertEqual(sources["delivery_source"], "prepared_only")
        self.assertFalse(sources["delivery_authoritative"])
        self.assertEqual(len(sources["undelivered_references"]), 2)
        self.assertEqual(review["attribution"]["independent_evidence_count"], 0)

    def test_role_step_budget_exhaustion_creates_nothing(self):
        root, flow = self.bound_root(budget_limit=2)
        self.run_step(root, flow, "event_analyst", "分析")
        self.run_step(root, flow, "counter_evidence", "反证")
        before = len(self.store.list_tasks())
        with self.assertRaises(RoleFlowError) as caught:
            self.run_step(root, flow, "synthesis_drafter", "综合")
        self.assertIn("step_rejected", str(caught.exception))
        self.assertIn("budget_exceeded", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), before,
                         "an over-budget step is refused with zero creation")
        self.assertEqual([task["role_id"] for task in self.children(root["task_id"])],
                         ["event_analyst", "counter_evidence"])
        self.assertEqual(self.store.get_task(root["task_id"])["attempt_count"], 0)


# ---------------------------------------------------------------------- #
# 循环与边界 / 重复与预算                                                 #
# ---------------------------------------------------------------------- #

class FanoutReviewTests(R22IntegrationCase):
    def parent(self, **kwargs):
        return self.create_root(**kwargs)

    def test_boundary_rejections_create_nothing(self):
        parent = self.parent()
        count = len(self.store.list_tasks())

        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=parent["task_id"],
                             proposals=[{"role_id": "event_analyst", "question": "q",
                                         "symbols": ["600519", "000001"]}])
        self.assertIn("scope_widened", str(caught.exception))

        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=parent["task_id"],
                             proposals=[{"role_id": "event_analyst", "question": "q",
                                         "case_id": "another-case"}])
        self.assertIn("cross_case", str(caught.exception))

        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=parent["task_id"],
                             proposals=[{"role_id": "rookie_writer", "question": "q"}])
        self.assertIn("role_not_allowed", str(caught.exception))

        # Legal fan-out at the cap, then one more is refused whole.
        legal = propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=[
                                     {"role_id": "event_analyst", "question": "分析"},
                                     {"role_id": "counter_evidence", "question": "反证"}])
        self.assertEqual(len(legal["created"]), 2)
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=parent["task_id"],
                             proposals=[{"role_id": "synthesis_drafter", "question": "综合"},
                                        {"role_id": "quality_check", "question": "质量"}])
        self.assertIn("fanout_exceeded", str(caught.exception))

        child = self.store.get_task(legal["created"][0]["task_id"])
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=child["task_id"],
                             proposals=[{"role_id": "counter_evidence", "question": "更深"}])
        self.assertIn("depth_exceeded", str(caught.exception))

        self.assertEqual(len(self.store.list_tasks()), count + 2,
                         "only the legal batch created rows")

    def test_dependency_review_orders_and_records_edges(self):
        parent = self.parent()
        result = propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                  proposals=[
                                      {"role_id": "synthesis_drafter", "question": "综合",
                                       "depends_on": ["event_analyst", "counter_evidence"]},
                                      {"role_id": "event_analyst", "question": "分析"},
                                      {"role_id": "counter_evidence", "question": "反证"}])
        by_role = {entry["role_id"]: entry for entry in result["created"]}
        self.assertEqual(sorted(by_role["synthesis_drafter"]["depends_on"]),
                         sorted([by_role["event_analyst"]["task_id"],
                                 by_role["counter_evidence"]["task_id"]]))
        synthesis = self.store.get_task(by_role["synthesis_drafter"]["task_id"])
        self.assertEqual(sorted(synthesis["input_payload"]["origin"]["depends_on"]),
                         sorted([by_role["event_analyst"]["task_id"],
                                 by_role["counter_evidence"]["task_id"]]))
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[parent["task_id"]])
        row = {item["task_id"]: item for item in review["tasks"]}[synthesis["task_id"]]
        self.assertEqual(sorted(row["relations"]["depends_on"]),
                         sorted(synthesis["input_payload"]["origin"]["depends_on"]))

    def test_dependency_cycles_and_unknown_refs_refused(self):
        parent = self.parent()
        count = len(self.store.list_tasks())
        for proposals in (
            [{"role_id": "event_analyst", "question": "a", "depends_on": ["event_analyst"]}],
            [{"role_id": "event_analyst", "question": "a", "depends_on": ["counter_evidence"]},
             {"role_id": "counter_evidence", "question": "b", "depends_on": ["event_analyst"]}],
            [{"role_id": "event_analyst", "question": "a", "depends_on": ["quality_check"]}],
            [{"role_id": "event_analyst", "question": "a", "depends_on": ["not-a-role"]}],
        ):
            with self.assertRaises(SubtaskGraphError) as caught:
                propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=proposals)
            self.assertIn("dependency_invalid", str(caught.exception))
            self.assertEqual(len(self.store.list_tasks()), count,
                             "a refused dependency creates nothing")

    def test_duplicate_submission_after_budget_exhaustion_meters_nothing(self):
        parent = self.parent(budget_limit=2)
        first = propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=[
                                     {"role_id": "event_analyst", "question": "分析",
                                      "budget_reserve": 2}],
                                 budget_limit=2)
        self.assertEqual(len(first["created"]), 1)
        count = len(self.store.list_tasks())
        # Same identity, same question (punctuation differs): a duplicate.
        again = propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=[
                                     {"role_id": "event_analyst",
                                      "question": " 分析！", "budget_reserve": 2}],
                                 budget_limit=2)
        self.assertEqual(again["created"], [])
        self.assertEqual([item["reason"] for item in again["duplicates"]],
                         ["duplicate_child"])
        self.assertEqual(again["duplicates"][0]["task_id"],
                         first["created"][0]["task_id"])
        self.assertEqual(len(self.store.list_tasks()), count,
                         "a duplicate after exhaustion adds no task and no reservation")
        child = self.store.get_task(first["created"][0]["task_id"])
        self.assertEqual(child["attempt_count"], 0, "duplicates never meter")

    def test_concurrent_identical_submissions_create_one_task(self):
        parent = self.parent()
        proposal = [{"role_id": "event_analyst", "question": "并发提交"}]

        def submit():
            store = TaskStore(self.db_path)
            try:
                return propose_subtasks(store, parent_task_id=parent["task_id"],
                                        proposals=proposal)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in (pool.submit(submit),
                                                      pool.submit(submit))]
        created = [entry for result in results for entry in result["created"]]
        duplicates = [entry for result in results for entry in result["duplicates"]]
        self.assertEqual(len(created), 1, "exactly one child is created")
        self.assertEqual(len(duplicates), 1, "the loser is answered as a duplicate")
        self.assertEqual(len(self.children(parent["task_id"])), 1)
        self.assertEqual(self.store.get_task(created[0]["task_id"])["attempt_count"], 0)
        self.assertEqual(duplicates[0]["task_id"], created[0]["task_id"])

    def _race(self, parent, left, right, **limits):
        """Both submitters read the same pre-lock snapshot, then race.

        The hook in ``list_tasks`` forces the interleaving the reviewer used:
        neither side's first children read may be the one that decides."""
        barrier = Barrier(2, timeout=10)

        class SynchronizedReadStore(TaskStore):
            def list_tasks(self, status=""):
                rows = super().list_tasks(status=status)
                barrier.wait()
                return rows

        def submit(proposal):
            connection = SynchronizedReadStore(self.db_path)
            try:
                try:
                    record = propose_subtasks(
                        connection, parent_task_id=parent["task_id"],
                        proposals=[proposal], **limits)
                    return ("created", len(record["created"]),
                            len(record["duplicates"]))
                except SubtaskGraphError as exc:
                    return ("refused", str(exc).split(":")[0].strip())
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return [future.result() for future in (pool.submit(submit, left),
                                                    pool.submit(submit, right))]

    def test_concurrent_distinct_roles_respect_cap_and_budget(self):
        parent = self.parent()
        results = self._race(
            parent,
            {"role_id": "event_analyst", "question": "first", "budget_reserve": 1},
            {"role_id": "counter_evidence", "question": "second", "budget_reserve": 1},
            budget_limit=1, max_children=1)
        self.assertEqual(len(self.children(parent["task_id"])), 1,
                         "two racing distinct proposals cannot exceed the cap")
        child = self.children(parent["task_id"])[0]
        self.assertEqual(child["input_payload"]["budget_reserve"], 1)
        refused = [result for result in results if result[0] == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn("fanout_exceeded", refused[0][1])

    def test_concurrent_same_identity_converges_on_one_task(self):
        parent = self.parent()
        results = self._race(
            parent,
            {"role_id": "event_analyst", "question": "same question"},
            {"role_id": "event_analyst", "question": "same question!"},
            budget_limit=1)
        self.assertEqual([result[0] for result in results], ["created", "created"],
                         "the loser answers a duplicate, not an idempotency error")
        self.assertEqual(len(self.children(parent["task_id"])), 1)
        self.assertEqual(sum(result[1] for result in results), 1)
        self.assertEqual(sum(result[2] for result in results), 1)

    def test_dependency_refused_even_when_proposal_is_a_duplicate(self):
        parent = self.parent()
        propose_subtasks(self.store, parent_task_id=parent["task_id"],
                         proposals=[{"role_id": "event_analyst", "question": "existing"}])
        before = len(self.store.list_tasks())
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=parent["task_id"],
                             proposals=[
                                 {"role_id": "event_analyst", "question": "existing",
                                  "depends_on": ["missing-role"]},
                                 {"role_id": "counter_evidence", "question": "new"}])
        self.assertIn("dependency_invalid", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), before,
                         "the invalid dependency vetoes the whole batch, dedupe or not")

    def test_dependency_batch_retry_stays_a_pure_duplicate(self):
        parent = self.parent()
        proposals = [
            {"role_id": "event_analyst", "question": "retry base"},
            {"role_id": "counter_evidence", "question": "retry dependent",
             "depends_on": ["event_analyst"]},
        ]
        first = propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=proposals)
        self.assertEqual(len(first["created"]), 2)
        count = len(self.store.list_tasks())
        again = propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=proposals)
        self.assertEqual(again["created"], [])
        self.assertEqual([item["reason"] for item in again["duplicates"]],
                         ["duplicate_child", "duplicate_child"])
        self.assertEqual(len(self.store.list_tasks()), count,
                         "an exact retry of a legal dependency batch meters nothing")

    def test_duplicate_of_existing_child_cannot_add_dependencies(self):
        parent = self.parent()
        propose_subtasks(self.store, parent_task_id=parent["task_id"],
                         proposals=[{"role_id": "event_analyst",
                                     "question": "existing"}])
        count = len(self.store.list_tasks())
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=parent["task_id"],
                             proposals=[{"role_id": "event_analyst",
                                         "question": "existing",
                                         "depends_on": ["counter_evidence"]}])
        self.assertIn("dependency_invalid", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), count,
                         "an existing child's recorded dependencies are immutable")

    def test_duplicate_line_cannot_redeclare_a_conflicting_dependency(self):
        parent = self.parent()
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(
                self.store, parent_task_id=parent["task_id"],
                proposals=[
                    {"role_id": "event_analyst", "question": "analysis"},
                    {"role_id": "counter_evidence", "question": "counter",
                     "depends_on": ["event_analyst"]},
                    # Same identity as line 1 but claiming the opposite edge:
                    # an exact retry may repeat dependencies, but a conflicting
                    # one would create a cycle if the edge were honoured.
                    {"role_id": "event_analyst", "question": "analysis",
                     "depends_on": ["counter_evidence"]},
                ])
        self.assertIn("dependency_invalid", str(caught.exception))
        self.assertEqual(self.children(parent["task_id"]), [],
                         "the conflicting duplicate vetoes the whole batch")

    def test_task_key_uses_the_full_normalized_question(self):
        parent = self.parent()
        prefix = "common question prefix" * 4
        result = propose_subtasks(
            self.store, parent_task_id=parent["task_id"],
            proposals=[{"role_id": "event_analyst", "question": prefix + " alpha"},
                       {"role_id": "event_analyst", "question": prefix + " bravo"}])
        self.assertEqual(len(result["created"]), 2,
                         "questions sharing their first 40 characters stay distinct")
        children = self.children(parent["task_id"])
        self.assertEqual(len(children), 2)
        self.assertEqual(
            sum(event["event_type"] == "task_created"
                for task in children for event in self.store.events(task["task_id"])),
            2)

    def test_read_only_store_refuses_creation(self):
        parent = self.parent()
        read_only = TaskStore(self.db_path, read_only=True)
        self.addCleanup(read_only.close)
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(read_only, parent_task_id=parent["task_id"],
                             proposals=[{"role_id": "event_analyst",
                                         "question": "read-only write check"}])
        self.assertIn("read_only_store", str(caught.exception))
        self.assertEqual(self.children(parent["task_id"]), [])

    def test_missing_cross_process_lock_refuses_creation(self):
        parent = self.parent()
        before = len(self.store.list_tasks())
        with patch.object(subtask_graph.fcntl, "flock",
                          side_effect=OSError("synthetic flock failure")):
            with self.assertRaises(SubtaskGraphError) as caught:
                propose_subtasks(self.store, parent_task_id=parent["task_id"],
                                 proposals=[{"role_id": "event_analyst",
                                             "question": "no lock"}])
        self.assertIn("graph_lock_unavailable", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), before,
                         "no cross-process exclusion means no creation")

    def test_in_memory_store_keeps_caller_connection_and_writes_no_sidecar(self):
        with tempfile.TemporaryDirectory() as path:
            previous = Path.cwd()
            os.chdir(path)
            store = TaskStore(":memory:")
            try:
                root, _ = store.create_task(
                    title="memory root", case_id="case-memory",
                    input_payload={"data_classification": "public"},
                    execution_mode="replay")
                result = propose_subtasks(
                    store, parent_task_id=root["task_id"],
                    proposals=[{"role_id": "event_analyst",
                                "question": "in-memory child"}])
                self.assertEqual(len(result["created"]), 1)
                self.assertEqual(store.get_task(root["task_id"])["task_id"],
                                 root["task_id"],
                                 "the caller's connection stays open")
                self.assertEqual(list(Path(path).glob("*.subtask.lock")), [],
                                 "an in-memory store writes no lock sidecar")
            finally:
                store.close()
                os.chdir(previous)


# ---------------------------------------------------------------------- #
# 父取消与部分失败                                                        #
# ---------------------------------------------------------------------- #

class CancelAndPartialFailureTests(R22IntegrationCase):
    def test_parent_cancel_propagates_and_preserves_terminal_child(self):
        root, flow = self.bound_root()
        self.run_step(root, flow, "event_analyst", "已完成的分析")
        pending = propose_subtasks(self.store, parent_task_id=root["task_id"],
                                   proposals=[{"role_id": "counter_evidence",
                                               "question": QUESTION}])
        pending_id = pending["created"][0]["task_id"]
        analyst = next(task for task in self.children(root["task_id"])
                       if task["role_id"] == "event_analyst")
        analyst_before = (analyst["status"], analyst["output"], analyst["attempt_count"])
        report = cancel_subtree(self.store, root["task_id"], actor="operator",
                                reason="用户取消整棵树")
        cancelled = {entry["task_id"] for entry in report["cancelled"]}
        self.assertIn(root["task_id"], cancelled)
        self.assertIn(pending_id, cancelled)
        self.assertNotIn(analyst["task_id"], cancelled,
                         "a terminal child keeps its history")
        self.assertEqual(self.store.get_task(analyst["task_id"])["status"], "succeeded")
        after = self.store.get_task(analyst["task_id"])
        self.assertEqual((after["status"], after["output"], after["attempt_count"]),
                         analyst_before)
        self.assertIn("task_cancelled",
                      [event["event_type"] for event in self.store.events(root["task_id"])])
        self.assertIn("task_cancelled",
                      [event["event_type"] for event in self.store.events(pending_id)])
        with self.assertRaises(RoleFlowError) as caught:
            finish_role_flow(self.store, root["task_id"], flow=flow,
                             registry=self.registry)
        self.assertIn("task_terminal", str(caught.exception))
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[root["task_id"]])
        rows = {row["task_id"]: row for row in review["tasks"]}
        self.assertEqual(rows[root["task_id"]]["status"], "cancelled")
        self.assertTrue(any("cancel" in item["event"]
                            for item in rows[pending_id]["failures"]))
        self.assertEqual(rows[analyst["task_id"]]["status"], "succeeded")

    def test_cancel_traverses_a_terminal_intermediate(self):
        root = self.create_root()
        middle = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "middle"}])
        middle_id = middle["created"][0]["task_id"]
        leaf = propose_subtasks(
            self.store, parent_task_id=middle_id, max_depth=2,
            proposals=[{"role_id": "counter_evidence", "question": "leaf"}])
        leaf_id = leaf["created"][0]["task_id"]
        claimed = self.store.claim_task(middle_id, "cancel-worker")
        token = claimed["fencing_token"]
        self.store.mark_running(middle_id, worker_id="cancel-worker",
                                fencing_token=token, expected_version=claimed["version"])
        self.store.complete(middle_id, worker_id="cancel-worker", fencing_token=token,
                            output={"summary": "synthetic completed intermediate"})
        report = cancel_subtree(self.store, root["task_id"], actor="operator",
                                reason="取消整棵树")
        cancelled = {entry["task_id"] for entry in report["cancelled"]}
        self.assertEqual(self.store.get_task(middle_id)["status"], "succeeded")
        self.assertNotIn(middle_id, cancelled, "the terminal node is not re-cancelled")
        self.assertIn(leaf_id, cancelled,
                      "a finished middle node must not shield its child")
        self.assertEqual(self.store.get_task(leaf_id)["status"], "cancelled")
        self.assertEqual(
            sum(event["event_type"] == "task_cancelled"
                for event in self.store.events(leaf_id)), 1)

    def test_workflow_cancel_entry_propagates_to_children(self):
        root = self.create_root()
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "queued child"}])
        child_id = child["created"][0]["task_id"]
        fresh = self.store.get_task(root["task_id"])
        cancelled_root = workflow_cancel_task(
            str(self.db_path), root["task_id"], actor="operator",
            reason="用户取消", expected_version=fresh["version"])
        self.assertEqual(cancelled_root["status"], "cancelled",
                         "the normal cancel entry still reports the root")
        self.assertEqual(self.store.get_task(child_id)["status"], "cancelled")
        self.assertEqual(
            sum(event["event_type"] == "task_cancelled"
                for event in self.store.events(child_id)), 1)

    def test_cancel_waits_for_an_in_flight_child_creation(self):
        root = self.create_root()
        entered, proceed = Event(), Event()
        original = TaskStore.create_task

        def pause_child_create(connection, **values):
            if values.get("parent_task_id") == root["task_id"]:
                entered.set()
                if not proceed.wait(5):
                    raise RuntimeError("test did not release the pending create")
            return original(connection, **values)

        with patch.object(TaskStore, "create_task", pause_child_create):
            with ThreadPoolExecutor(max_workers=2) as pool:
                making = pool.submit(
                    propose_subtasks, self.store,
                    parent_task_id=root["task_id"],
                    proposals=[{"role_id": "event_analyst",
                                "question": "pending child"}])
                self.assertTrue(entered.wait(5))
                cancelling = pool.submit(
                    workflow_cancel_task, str(self.db_path), root["task_id"],
                    actor="operator", reason="取消", expected_version=root["version"])
                proceed.set()
                making.result(timeout=8)
                cancelled_root = cancelling.result(timeout=8)
        children = self.children(root["task_id"])
        self.assertEqual(len(children), 1)
        self.assertEqual(cancelled_root["status"], "cancelled")
        self.assertTrue(children[0]["cancel_requested"],
                        "creation inside the boundary is visible to the cancel")
        self.assertEqual(self.store.get_task(children[0]["task_id"])["status"],
                         "cancelled")

    def test_cancel_absorbs_a_worker_claim_version_race(self):
        root = self.create_root()
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "leased child"}])
        child_id = child["created"][0]["task_id"]
        claimed = []
        original = TaskStore.request_cancel

        def claim_before_cancel(connection, task_id, **values):
            if task_id == child_id and not claimed:
                worker = TaskStore(self.db_path)
                try:
                    claimed.append(bool(worker.claim_task(child_id, "test-worker")))
                finally:
                    worker.close()
            return original(connection, task_id, **values)

        with patch.object(TaskStore, "request_cancel", claim_before_cancel):
            cancelled_root = workflow_cancel_task(
                str(self.db_path), root["task_id"], actor="operator",
                reason="取消", expected_version=root["version"])
        self.assertEqual(claimed, [True])
        self.assertEqual(cancelled_root["status"], "cancelled")
        child_after = self.store.get_task(child_id)
        self.assertEqual(child_after["status"], "leased")
        self.assertTrue(child_after["cancel_requested"],
                        "the retry records the flag on the freshly leased version")
        self.assertEqual(
            sum(event["event_type"] in {"task_cancelled", "task_cancel_requested"}
                for event in self.store.events(child_id)), 1)

    def test_proposal_refused_after_cancel_request_on_a_running_parent(self):
        root = self.create_root()
        claim = self.store.claim_task(root["task_id"], "cancel-worker")
        running = self.store.mark_running(
            root["task_id"], worker_id="cancel-worker",
            fencing_token=claim["fencing_token"], expected_version=claim["version"])
        requested = workflow_cancel_task(
            str(self.db_path), root["task_id"], actor="operator",
            reason="取消", expected_version=running["version"])
        self.assertEqual(requested["status"], "running")
        self.assertTrue(requested["cancel_requested"],
                        "a leased/running task records the request, not a status")
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=root["task_id"],
                             proposals=[{"role_id": "event_analyst",
                                         "question": "late work"}])
        self.assertIn("parent_cancel_requested", str(caught.exception))
        self.assertEqual(self.children(root["task_id"]), [],
                         "a pending cancellation admits no new work")
        terminal = self.store.cancel_if_requested(
            root["task_id"], worker_id="cancel-worker",
            fencing_token=claim["fencing_token"])
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(self.children(root["task_id"]), [],
                         "nothing appears after the worker's checkpoint either")

    def test_proposal_refused_under_a_cancelled_ancestor(self):
        root = self.create_root()
        middle = propose_subtasks(self.store, parent_task_id=root["task_id"],
                                  max_depth=3,
                                  proposals=[{"role_id": "event_analyst",
                                              "question": "middle"}])
        middle_id = middle["created"][0]["task_id"]
        claim = self.store.claim_task(middle_id, "ancestor-worker")
        self.store.mark_running(middle_id, worker_id="ancestor-worker",
                                fencing_token=claim["fencing_token"],
                                expected_version=claim["version"])
        # Cancel ONLY the root through the raw store entry: the middle node
        # carries no flag of its own, its ANCESTOR is cancelled.
        self.store.request_cancel(
            root["task_id"], actor="operator", reason="取消",
            expected_version=self.store.get_task(root["task_id"])["version"])
        with self.assertRaises(SubtaskGraphError) as caught:
            propose_subtasks(self.store, parent_task_id=middle_id, max_depth=3,
                             proposals=[{"role_id": "counter_evidence",
                                         "question": "grandchild"}])
        self.assertIn("parent_cancel_requested", str(caught.exception),
                      "an ancestor's cancellation blocks fan-out below it")
        self.assertEqual(self.children(middle_id), [])

    def test_partial_cancellation_resumes_with_the_current_root_version(self):
        root = self.create_root()
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "leased child"}])
        child_id = child["created"][0]["task_id"]
        claim = self.store.claim_task(child_id, "renewing-worker")
        original = TaskStore.request_cancel
        renewals = [0]

        def renew_before_cancel(connection, task_id, **values):
            if task_id == child_id:
                renewals[0] += 1
                self.store.renew_lease(child_id, worker_id="renewing-worker",
                                       fencing_token=claim["fencing_token"])
            return original(connection, task_id, **values)

        with patch.object(TaskStore, "request_cancel", renew_before_cancel):
            with self.assertRaises(SubtaskGraphError) as caught:
                workflow_cancel_task(
                    str(self.db_path), root["task_id"], actor="operator",
                    reason="取消", expected_version=root["version"])
        self.assertIn("cancel_conflict", str(caught.exception))
        self.assertGreaterEqual(renewals[0], 8)
        partial_root = self.store.get_task(root["task_id"])
        self.assertEqual(partial_root["status"], "cancelled",
                         "the authorized root transition commits first")
        self.assertFalse(self.store.get_task(child_id)["cancel_requested"],
                         "the descendant pass did not get through")
        resumed = workflow_cancel_task(
            str(self.db_path), root["task_id"], actor="operator",
            reason="取消", expected_version=partial_root["version"])
        self.assertEqual(resumed["status"], "cancelled")
        child_after = self.store.get_task(child_id)
        self.assertTrue(child_after["cancel_requested"])
        self.assertEqual(
            sum(event["event_type"] in {"task_cancelled", "task_cancel_requested"}
                for event in self.store.events(child_id)), 1,
            "the resumed pass is idempotent — one event per descendant")
        self.assertEqual(
            sum(event["event_type"] == "task_cancelled"
                for event in self.store.events(root["task_id"])), 1,
            "the resume never re-emits the root cancellation")

    def test_personal_workspace_cancel_cancels_the_children(self):
        created = create_personal_research(
            self.store, actor=OWNER, question="个人工作台取消整棵树 600519",
            symbols=["600519"], execution_mode="replay", config=self.config)
        root = created["task"]
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "个人子任务"}])
        child_id = child["created"][0]["task_id"]
        with self.assertRaises(TaskVersionConflictError):
            cancel_personal_task(self.store, actor=OWNER, task_id=root["task_id"],
                                 expected_version=0, reason="版本不符")
        self.assertEqual(self.store.get_task(root["task_id"])["status"], "queued",
                         "a stale version cancels nothing")
        cancelled = cancel_personal_task(
            self.store, actor=OWNER, task_id=root["task_id"],
            expected_version=self.store.get_task(root["task_id"])["version"],
            reason="用户取消")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.get_task(child_id)["status"], "cancelled",
                         "the workspace entry cannot leave a child queued")
        self.assertEqual(
            sum(event["event_type"] == "task_cancelled"
                for event in self.store.events(child_id)), 1)
        # A repeated call with the current version is the idempotent resume of
        # an already-cancelled root: success, and no duplicate child event.
        again = cancel_personal_task(
            self.store, actor=OWNER, task_id=root["task_id"],
            expected_version=cancelled["version"], reason="再次取消")
        self.assertEqual(again["status"], "cancelled")
        self.assertEqual(
            sum(event["event_type"] == "task_cancelled"
                for event in self.store.events(child_id)), 1,
            "the resume never duplicates a descendant cancellation")
        with self.assertRaises(TaskVersionConflictError):
            cancel_personal_task(self.store, actor=OWNER, task_id=root["task_id"],
                                 expected_version=0, reason="过期版本")

    def test_workspace_cancel_still_refuses_finished_roots(self):
        created = create_personal_research(
            self.store, actor=OWNER, question="个人工作台拒绝已完成任务 600519",
            symbols=["600519"], execution_mode="replay", config=self.config)
        root = created["task"]
        claim = self.store.claim_task(root["task_id"], "finished-worker")
        self.store.mark_running(root["task_id"], worker_id="finished-worker",
                                fencing_token=claim["fencing_token"],
                                expected_version=claim["version"])
        self.store.complete(root["task_id"], worker_id="finished-worker",
                            fencing_token=claim["fencing_token"],
                            output={"summary": "synthetic finished result"})
        finished = self.store.get_task(root["task_id"])
        with self.assertRaises(WorkspaceError) as caught:
            cancel_personal_task(self.store, actor=OWNER, task_id=root["task_id"],
                                 expected_version=finished["version"])
        self.assertIn("cancel_terminal", str(caught.exception),
                      "only a cancelled root is resumable; finished work is not")

    def test_workspace_entry_resumes_an_interrupted_cancellation(self):
        created = create_personal_research(
            self.store, actor=OWNER, question="个人工作台恢复取消 600519",
            symbols=["600519"], execution_mode="replay", config=self.config)
        root = created["task"]
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "个人续跑子任务"}])
        child_id = child["created"][0]["task_id"]
        claim = self.store.claim_task(child_id, "resume-worker")
        original = TaskStore.request_cancel
        renewals = [0]

        def renew_before_cancel(connection, task_id, **values):
            if task_id == child_id:
                renewals[0] += 1
                self.store.renew_lease(child_id, worker_id="resume-worker",
                                       fencing_token=claim["fencing_token"])
            return original(connection, task_id, **values)

        with patch.object(TaskStore, "request_cancel", renew_before_cancel):
            with self.assertRaises(SubtaskGraphError) as caught:
                cancel_personal_task(
                    self.store, actor=OWNER, task_id=root["task_id"],
                    expected_version=root["version"], reason="用户取消")
        self.assertIn("cancel_conflict", str(caught.exception))
        self.assertGreaterEqual(renewals[0], 8)
        partial_root = self.store.get_task(root["task_id"])
        self.assertEqual(partial_root["status"], "cancelled")
        self.assertFalse(self.store.get_task(child_id)["cancel_requested"])
        resumed = cancel_personal_task(
            self.store, actor=OWNER, task_id=root["task_id"],
            expected_version=partial_root["version"], reason="用户取消")
        self.assertEqual(resumed["status"], "cancelled")
        child_after = self.store.get_task(child_id)
        self.assertTrue(child_after["cancel_requested"],
                        "the workspace entry finishes the interrupted descent")
        self.assertEqual(
            sum(event["event_type"] in {"task_cancelled", "task_cancel_requested"}
                for event in self.store.events(child_id)), 1)

    def test_protected_resume_consumes_its_own_receipt(self):
        import sqlite3
        from datetime import datetime, timedelta

        def receipt(label):
            key = "resume-{0}".format(label)
            self.store.register_principal_verification(
                key, actor="operator", task_id=root["task_id"], action="cancel",
                expires_at=(datetime.now().astimezone()
                            + timedelta(minutes=5)).isoformat())
            return {"verification_id": key, "actor": "operator", "action": "cancel",
                    "command_id": "cancel:{0}".format(root["task_id"])}

        root = self.create_root()
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "protected child"}])
        child_id = child["created"][0]["task_id"]
        claim = self.store.claim_task(child_id, "protected-worker")
        original = TaskStore.request_cancel
        renew = {"on": True}

        def renew_before_cancel(connection, task_id, **values):
            if task_id == child_id and renew["on"]:
                self.store.renew_lease(child_id, worker_id="protected-worker",
                                       fencing_token=claim["fencing_token"])
            return original(connection, task_id, **values)

        initial, recovery = receipt("initial"), receipt("recovery")
        with patch.object(TaskStore, "request_cancel", renew_before_cancel):
            with self.assertRaises(SubtaskGraphError):
                workflow_cancel_task(
                    str(self.db_path), root["task_id"], actor="operator",
                    reason="取消", expected_version=root["version"],
                    principal_verification=initial)
        partial_root = self.store.get_task(root["task_id"])
        self.assertEqual(partial_root["status"], "cancelled")
        renew["on"] = False
        resumed = workflow_cancel_task(
            str(self.db_path), root["task_id"], actor="operator",
            reason="取消", expected_version=partial_root["version"],
            principal_verification=recovery)
        self.assertEqual(resumed["status"], "cancelled")
        self.assertTrue(self.store.get_task(child_id)["cancel_requested"])
        with sqlite3.connect(Path(self.db_path).resolve().as_uri() + "?mode=ro",
                             uri=True) as reader:
            used = dict(reader.execute(
                "SELECT verification_id, used_at FROM principal_verifications "
                "WHERE verification_id IN (?,?)",
                [initial["verification_id"], recovery["verification_id"]]).fetchall())
        self.assertTrue(used.get(initial["verification_id"]),
                        "the first receipt authorized the committed root cancel")
        self.assertTrue(used.get(recovery["verification_id"]),
                        "the resume's receipt is consumed and COMMITTED")

    def test_single_task_cancel_survives_missing_fcntl(self):
        root = self.create_root()
        other = self.create_root()
        with patch.object(subtask_graph, "fcntl", None):
            cancelled = workflow_cancel_task(
                str(self.db_path), root["task_id"], actor="operator",
                reason="无 fcntl 取消", expected_version=root["version"])
            with self.assertRaises(SubtaskGraphError) as caught:
                propose_subtasks(
                    self.store, parent_task_id=other["task_id"],
                    proposals=[{"role_id": "event_analyst", "question": "no lock"}])
        self.assertEqual(cancelled["status"], "cancelled",
                         "cancellation must not depend on the graph file lock")
        self.assertIn("graph_lock_unavailable", str(caught.exception),
                      "creation still refuses without cross-process exclusion")
        self.assertEqual(
            sum(event["event_type"] == "task_cancelled"
                for event in self.store.events(root["task_id"])), 1)

    def test_cancel_fails_closed_when_the_file_lock_errors(self):
        if subtask_graph.fcntl is None:
            self.skipTest("this host has no POSIX advisory locking module")
        root = self.create_root()
        other = self.create_root()
        with patch.object(subtask_graph.fcntl, "flock",
                          side_effect=OSError("synthetic advisory lock failure")):
            with self.assertRaises(SubtaskGraphError) as cancel_failure:
                workflow_cancel_task(
                    str(self.db_path), root["task_id"], actor="operator",
                    reason="锁错误取消", expected_version=root["version"])
        self.assertIn("graph_lock_unavailable", str(cancel_failure.exception))
        self.assertEqual(self.store.get_task(root["task_id"])["status"], "queued",
                         "cancellation never proceeds without the interlock")
        self.assertEqual(
            sum(event["event_type"] in {"task_cancelled", "task_cancel_requested"}
                for event in self.store.events(root["task_id"])), 0)
        # Creation keeps its fail-closed behaviour; the module still exists.
        with patch.object(subtask_graph.fcntl, "flock",
                          side_effect=OSError("synthetic advisory lock failure")):
            with self.assertRaises(SubtaskGraphError) as create_failure:
                propose_subtasks(
                    self.store, parent_task_id=other["task_id"],
                    proposals=[{"role_id": "event_analyst", "question": "locked"}])
        self.assertIn("graph_lock_unavailable", str(create_failure.exception))

    def test_partial_step_failure_keeps_sibling_parent_and_audit(self):
        root, flow = self.bound_root()
        self.run_step(root, flow, "event_analyst", "兄弟步骤的成功结果")
        analyst = next(task for task in self.children(root["task_id"])
                       if task["role_id"] == "event_analyst")
        analyst_before = (analyst["status"], analyst["output"], analyst["attempt_count"])
        first = execute_role_step(
            self.store, root["task_id"], role_id="counter_evidence", flow=flow,
            registry=self.registry, config=self.config,
            step_overrides={"replay_response": None})
        self.assertEqual(first["status"], "queued",
                         "the first withheld answer requeues within the attempt budget")
        second = execute_role_step(
            self.store, root["task_id"], role_id="counter_evidence", flow=flow,
            registry=self.registry, config=self.config,
            step_overrides={"replay_response": None})
        self.assertEqual(second["status"], "failed")
        with self.assertRaises(RoleFlowError) as caught:
            execute_role_step(
                self.store, root["task_id"], role_id="counter_evidence", flow=flow,
                registry=self.registry, config=self.config,
                step_overrides={"replay_response": replay("不会执行", [self.doc_id])})
        self.assertIn("step_failed_needs_decision", str(caught.exception))

        counter = self.store.get_task(second["task_id"])
        self.assertEqual(counter["status"], "failed")
        self.assertEqual(counter["attempt_count"], 2)
        self.assertIn("task_failed",
                      [event["event_type"] for event in self.store.events(counter["task_id"])])
        after = self.store.get_task(analyst["task_id"])
        self.assertEqual((after["status"], after["output"], after["attempt_count"]),
                         analyst_before, "the sibling is untouched")
        root_after = self.store.get_task(root["task_id"])
        self.assertEqual(root_after["status"], "queued", "the parent keeps its state")
        self.assertEqual({task["role_id"] for task in self.children(root["task_id"])},
                         {"event_analyst", "counter_evidence"},
                         "a failing step does not cascade or duplicate")

        review = project_review(self.store, identity=self.identity(),
                                task_ids=[root["task_id"]])
        rows = {row["task_id"]: row for row in review["tasks"]}
        failed_row = rows[counter["task_id"]]
        self.assertEqual(failed_row["status"], "failed")
        self.assertEqual(failed_row["metering"]["attempts"], 2)
        self.assertTrue(any("failed" in item["event"] for item in failed_row["failures"]))
        self.assertEqual(rows[analyst["task_id"]]["result"]["summary"],
                         str(analyst_before[1].get("summary") or ""))


# ---------------------------------------------------------------------- #
# 投递账本与签收语义                                                       #
# ---------------------------------------------------------------------- #

class SignoffAndDeliveryTests(R22IntegrationCase):
    def attempt_fixture(self, test_name):
        """A live-worker fixture (R02) so the invocation ledger is real."""
        from test_refactor_r02 import AttemptChainTests

        fixture = AttemptChainTests(test_name)
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_projection_uses_the_invocation_ledger_on_a_known_failure(self):
        fixture = self.attempt_fixture("test_known_provider_failure_is_not_auto_retried")
        task = fixture.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=OSError("HTTP Error 401: Unauthorized")) as stub:
            fixture.run_worker(task)
        self.assertEqual(stub.call_count, 1)
        attempts = fixture.store.invocation_attempts(task["task_id"])
        self.assertEqual([(item["state"], item["sent"]) for item in attempts],
                         [("failed_known", "yes")])
        review = project_review(fixture.store, identity=self.identity(),
                                task_ids=[task["task_id"]])
        sources = review["tasks"][0]["sources"]
        self.assertEqual(sources["delivery_state"], "sent",
                         "a deterministic 401 still DELIVERED the outbound context")
        self.assertTrue(sources["delivery_authoritative"])
        self.assertEqual([(item["round"], item["sent"], item["state"])
                          for item in sources["delivery_rounds"]],
                         [(1, "yes", "sent")])
        self.assertTrue(sources["delivery_rounds"][0]["attempt_id"],
                        "the context summary is bound to the round's attempt row")

    def test_latest_unknown_send_survives_an_earlier_successful_round(self):
        from test_refactor_r02 import _pi_error

        fixture = self.attempt_fixture(
            "test_a07_timeout_after_send_is_unknown_and_never_auto_requeued")
        task = fixture.create_live_task()
        first_round = dict(fixture.final_output(task["task_id"]), requested_intents=[{
            "schema_version": "1.0", "task_id": task["task_id"],
            "action": "read_public_quote_snapshot", "parameters": {"symbol": "600519"},
            "reason_summary": "synthetic round continuation",
        }])
        timeout = _pi_error("synthetic timeout", stage="process", sent_hint="unknown")
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[fixture.response(first_round), timeout]) as stub:
            result = fixture.run_worker(task)
        self.assertEqual(stub.call_count, 2)
        self.assertEqual(result["status"], "blocked")
        attempts = sorted(fixture.store.invocation_attempts(task["task_id"]),
                          key=lambda item: item["round_index"])
        self.assertEqual([item["sent"] for item in attempts], ["yes", "unknown"])
        review = project_review(fixture.store, identity=self.identity(),
                                task_ids=[task["task_id"]])
        sources = review["tasks"][0]["sources"]
        self.assertEqual(sources["delivery_state"], "send_unknown",
                         "the global called flag must not turn the LAST send into 'sent'")
        self.assertFalse(sources["delivery_authoritative"])
        self.assertEqual(sources["delivered_evidence"], {},
                         "an unknown send never admits evidence as delivered")
        self.assertEqual([(item["round"], item["sent"], item["state"])
                          for item in sources["delivery_rounds"]],
                         [(1, "yes", "sent"), (2, "unknown", "send_unknown")])

    def test_context_round_never_pairs_with_an_earlier_runs_attempt(self):
        from stock_agent.personal_review import _delivery

        payload = {"public_context": {"evidence_summaries": {
            "doc-1": {"summary": "synthetic", "sha256": "0" * 64}}}}
        model_call = {
            "mode": "live",
            "error": "ModelCallNotSent: synthetic preflight failure",
            "context_rounds": [{
                "round": 1, "prepared_at": "2026-11-24T10:00:10+08:00",
                "delivery": {"delivered_evidence": ["doc-1"]}}],
            "context_delivery": {"delivered_evidence": ["doc-1"]},
            "outbound_context": {"evidence_summaries": {
                "doc-1": {"summary": "synthetic", "sha256": "0" * 64}}},
        }
        earlier_run = [{
            "attempt_id": "att-previous-run", "round_index": 1, "sent": "yes",
            "started_at": "2026-11-24T10:00:00+08:00"}]
        delivery = _delivery(payload, model_call, earlier_run)
        self.assertEqual(delivery["state"], "prepared_only",
                         "an older run's attempt cannot vouch for a new context")
        self.assertEqual(delivery["delivered"], {})
        self.assertFalse(delivery["authoritative"])

    def test_same_second_requeue_never_borrows_the_prior_lease_send(self):
        from datetime import datetime

        from stock_agent.model_operations import LLMResponse

        fixture = self.attempt_fixture(
            "test_a06_not_sent_failure_requeues_within_attempt_budget")
        fixture.config.setdefault("task_runtime", {})["budget"] = {
            "daily_model_call_limit": 1}
        task = fixture.create_live_task()
        fixed_second = datetime.now().astimezone().isoformat(timespec="seconds")
        with patch("stock_agent.task_runtime.store._now", return_value=fixed_second), \
                patch("stock_agent.research_worker.now_iso", return_value=fixed_second), \
                patch("stock_agent.pi_runtime.call_pi_agent",
                      return_value=LLMResponse("synthetic invalid JSON",
                                               {"model": "offline-stub"})) as stub:
            first = fixture.run_worker(task)
            again = fixture.store.claim_task(task["task_id"], "w-r02")
            self.assertTrue(again, "the invalid answer requeues within budget")
            second = fixture.run_worker(task)
        attempts = fixture.store.invocation_attempts(task["task_id"])
        self.assertEqual(len(attempts), 1)
        self.assertLess(attempts[0]["fencing_token"], again["fencing_token"],
                        "the only invocation belongs to the PREVIOUS lease")
        latest = second["model_call"]["context_rounds"][-1]
        self.assertFalse(latest.get("attempt_id"))
        self.assertEqual(latest["prepared_at"], attempts[0]["started_at"],
                         "same round, same second — time is not identity")
        review = project_review(fixture.store, identity=self.identity(),
                                task_ids=[task["task_id"]])
        sources = review["tasks"][0]["sources"]
        self.assertEqual(sources["delivery_state"], "prepared_only",
                         "a prior lease's send never vouches for the new context")
        self.assertEqual(sources["delivered_evidence"], {})
        self.assertFalse(sources["delivery_authoritative"])
        self.assertEqual(stub.call_count, 1,
                         "the budget blocked the second dispatch before any send")

    def test_cancelled_ancestor_blocks_dispatch_before_recovery(self):
        fixture = self.attempt_fixture("test_successful_call_records_succeeded_attempt")
        store = fixture.store
        root = fixture.create_live_task()
        root = store.get_task(root["task_id"])
        child = propose_subtasks(
            store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "synthetic child research"}])
        child_id = child["created"][0]["task_id"]
        claim = store.claim_task(child_id, "w-r02")
        original = TaskStore.request_cancel

        def renew_before_cancel(connection, task_id, **values):
            if task_id == child_id:
                store.renew_lease(child_id, worker_id="w-r02",
                                  fencing_token=claim["fencing_token"])
            return original(connection, task_id, **values)

        # The root commits cancelled; the child pass exhausts its retries, so
        # the child is still leased with NO cancel flag yet.
        with patch.object(TaskStore, "request_cancel", renew_before_cancel):
            with self.assertRaises(SubtaskGraphError):
                workflow_cancel_task(
                    str(store.db_path), root["task_id"], actor="operator",
                    reason="取消", expected_version=root["version"])
        partial_child = store.get_task(child_id)
        self.assertEqual(partial_child["status"], "leased")
        self.assertFalse(partial_child["cancel_requested"])
        # A normal worker starts BEFORE the recovery pass: it must not spend a
        # call or write an invocation row for a cancelled tree.
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=fixture.response(
                       fixture.final_output(child_id))) as stub:
            result = fixture.run_worker(partial_child)
        self.assertEqual(stub.call_count, 0,
                         "a cancelled ancestor forbids every outbound call")
        self.assertEqual(store.invocation_attempts(child_id), [],
                         "no invocation row is written in the recovery window")
        self.assertEqual(result["status"], "cancelled")
        child_after = store.get_task(child_id)
        self.assertEqual(child_after["status"], "cancelled")
        self.assertTrue(child_after["cancel_requested"],
                        "the worker records the persistent cancellation marker")
        self.assertEqual(store.get_task(child_id)["output"], {})

    def test_cancel_committed_at_the_prompt_check_never_reserves_a_call(self):
        from stock_agent import research_worker as worker_module
        from test_refactor_r02 import _spec

        fixture = self.attempt_fixture("test_successful_call_records_succeeded_attempt")
        store = fixture.store
        root, _ = store.create_task(
            title="Synthetic root", case_id="synthetic",
            idempotency_key="synthetic-prompt-check-root",
            input_payload=_spec(), execution_mode="live")
        child = propose_subtasks(
            store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "synthetic child research"}])
        child_id = child["created"][0]["task_id"]
        store.claim_task(child_id, "w-r02")
        gate = worker_module.assert_payload_external_safe
        committed = {}

        def cancel_at_prompt_check(prompt):
            # The LAST checkpoint before this round's budget reservation and
            # dispatch: exactly the window the counterexample uses.
            gate(prompt)
            if committed:
                return
            parent = workflow_cancel_task(
                str(store.db_path), root["task_id"], actor="operator",
                reason="取消", expected_version=root["version"])
            committed.update({
                "root": parent["status"],
                "child_cancel_requested": bool(
                    store.get_task(child_id)["cancel_requested"]),
                "invocations": len(store.invocation_attempts(child_id)),
            })

        with patch.object(worker_module, "assert_payload_external_safe",
                          cancel_at_prompt_check), \
                patch("stock_agent.pi_runtime.call_pi_agent",
                      return_value=fixture.response(
                          fixture.final_output(child_id))) as stub:
            result = run_research_task(store, child_id, worker_id="w-r02",
                                       config=fixture.config)
        self.assertEqual(committed["root"], "cancelled")
        self.assertTrue(committed["child_cancel_requested"])
        self.assertEqual(committed["invocations"], 0)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(stub.call_count, 0,
                         "a cancellation committed before reservation forbids the call")
        self.assertEqual(store.invocation_attempts(child_id), [],
                         "no invocation row follows a committed cancellation")
        self.assertEqual(
            sum(event["event_type"] == "research_attempt_started"
                for event in store.events(child_id)), 0,
            "no budget reservation is written after the cancellation committed")
        self.assertEqual(store.get_task(child_id)["status"], "cancelled")

    def test_workspace_cancel_refuses_a_foreign_owner(self):
        created = create_personal_research(
            self.store, actor=OWNER, question="个人工作台归属校验 600519",
            symbols=["600519"], execution_mode="replay", config=self.config)
        root = created["task"]
        child = propose_subtasks(
            self.store, parent_task_id=root["task_id"],
            proposals=[{"role_id": "event_analyst", "question": "归属校验子任务"}])
        child_id = child["created"][0]["task_id"]
        self.assertTrue(self.store.input_owner_binding(root["task_id"]))
        with self.assertRaises(WorkspaceError) as caught:
            cancel_personal_task(self.store, actor=OTHER, task_id=root["task_id"],
                                 expected_version=root["version"], reason="越权取消")
        self.assertIn("personal_owner_mismatch", str(caught.exception))
        self.assertEqual(self.store.get_task(root["task_id"])["status"], "queued")
        self.assertEqual(self.store.get_task(child_id)["status"], "queued")
        for task_id in (root["task_id"], child_id):
            self.assertEqual(
                sum(event["event_type"] in {"task_cancelled", "task_cancel_requested"}
                    for event in self.store.events(task_id)), 0,
                "a refused owner writes no cancellation event")

    def test_legacy_model_call_never_borrows_a_prior_lease_send(self):
        from stock_agent.personal_review import _delivery

        payload = {"public_context": {"evidence_summaries": {
            "doc-1": {"summary": "synthetic", "sha256": "0" * 64}}}}
        outbound = {"evidence_summaries": {
            "doc-1": {"summary": "synthetic", "sha256": "0" * 64}}}
        attempts = [{"attempt_id": "att-old-lease", "round_index": 1, "sent": "yes",
                     "started_at": "2026-11-24T10:00:00+08:00",
                     "worker_id": "w-r02", "fencing_token": 3}]
        legacy_blocked = {
            "mode": "live", "called": False, "rounds": [],
            "context_delivery": {"delivered_evidence": ["doc-1"]},
            "outbound_context": outbound,
            "governance": {"gate": "budget"},
        }
        delivery = _delivery(payload, legacy_blocked, attempts)
        self.assertEqual(delivery["state"], "prepared_only",
                         "a legacy record after requeue has no send credential")
        self.assertEqual(delivery["delivered"], {})
        self.assertFalse(delivery["authoritative"])
        self.assertEqual(delivery["rounds"], [])
        # The historical invocation stays visible in the ledger, but even a
        # legacy record that DID return cannot bind its refs: the top-level
        # context is overwritten every round, so a later budget-denied round's
        # reference set could be attributed to this send.
        legacy_delivered = dict(legacy_blocked, called=True,
                                rounds=[{"round": 1, "attempt_id": "att-old-lease"}])
        delivered = _delivery(payload, legacy_delivered, attempts)
        self.assertEqual(delivered["state"], "prepared_only")
        self.assertFalse(delivered["authoritative"])
        self.assertEqual(delivered["delivered"], {})
        self.assertEqual(delivered["rounds"], [])

    def test_legacy_earlier_round_record_never_vouches_for_the_latest_context(self):
        fixture = self.attempt_fixture(
            "test_a07_timeout_after_send_is_unknown_and_never_auto_requeued")
        store = fixture.store
        task = fixture.create_live_task()
        task_id = task["task_id"]
        token = int(store.get_task(task_id)["fencing_token"])
        rows = []
        for round_index, sha, state, sent in (
                (1, "a" * 64, "succeeded", "yes"),
                (2, "b" * 64, "outcome_unknown", "unknown")):
            attempt = store.prepare_invocation_attempt(
                task_id, worker_id="w-r02", fencing_token=token,
                round_index=round_index, provider="stub", request_model="stub",
                prompt_sha256=sha)
            store.mark_invocation_dispatched(attempt["attempt_id"],
                                             worker_id="w-r02", fencing_token=token)
            store.complete_invocation_attempt(
                attempt["attempt_id"], worker_id="w-r02", fencing_token=token,
                state=state, sent=sent)
            rows.append(attempt)
        # A legacy writer's record: rounds holds only the FIRST successful
        # round, while the latest context's send went unknown.
        legacy = {
            "mode": "live", "called": True,
            "rounds": [{"round": 1, "attempt_id": rows[0]["attempt_id"],
                        "prompt_sha256": "a" * 64}],
            "context_delivery": {"delivered_evidence": ["doc-1"]},
            "outbound_context": {"evidence_summaries": {
                "doc-1": {"summary": "synthetic", "sha256": "0" * 64}}},
            "error": "ModelOutcomeUnknown: synthetic timeout after dispatch",
        }
        store.mark_blocked(task_id, worker_id="w-r02", fencing_token=token,
                           error=legacy["error"], model_call=legacy)
        row = project_review(store, identity=self.identity(),
                             task_ids=[task_id])["tasks"][0]
        sources = row["sources"]
        self.assertEqual(sources["delivery_state"], "send_unknown",
                         "an earlier round's record cannot vouch for the last context")
        self.assertFalse(sources["delivery_authoritative"])
        self.assertEqual(sources["delivered_evidence"], {})
        self.assertEqual(sources["delivery_rounds"], [])
        self.assertEqual(row["metering"]["invocation_attempts"], 2,
                         "the historical invocation ledger stays visible")
        self.assertEqual(row["metering"]["invocation_sent"], ["unknown", "yes"])

    def test_legacy_budget_denied_round_never_marks_prepared_refs_delivered(self):
        fixture = self.attempt_fixture(
            "test_a07_timeout_after_send_is_unknown_and_never_auto_requeued")
        store = fixture.store
        task = fixture.create_live_task()
        task_id = task["task_id"]
        token = int(store.get_task(task_id)["fencing_token"])
        first = store.prepare_invocation_attempt(
            task_id, worker_id="w-r02", fencing_token=token, round_index=1,
            provider="stub", request_model="stub", prompt_sha256="a" * 64)
        store.mark_invocation_dispatched(first["attempt_id"], worker_id="w-r02",
                                         fencing_token=token)
        store.complete_invocation_attempt(first["attempt_id"], worker_id="w-r02",
                                          fencing_token=token, state="succeeded",
                                          sent="yes")
        # Round two prepared doc-1 AND doc-2, then hit the shared budget before
        # an invocation row existed: the ledger holds only round one, but the
        # legacy record's top-level context is the round-two set.
        legacy = {
            "mode": "live", "called": True,
            "rounds": [{"round": 1, "attempt_id": first["attempt_id"],
                        "prompt_sha256": "a" * 64}],
            "context_delivery": {"delivered_evidence": ["doc-1", "doc-2"]},
            "outbound_context": {"evidence_summaries": {
                "doc-1": {"summary": "synthetic A", "sha256": "0" * 64},
                "doc-2": {"summary": "synthetic B", "sha256": "1" * 64}}},
            "governance": {"gate": "budget", "decision": "denied"},
            "error": "daily model-call budget exhausted: 1/1 attempts today",
        }
        store.mark_blocked(task_id, worker_id="w-r02", fencing_token=token,
                           error=legacy["error"], model_call=legacy)
        row = project_review(store, identity=self.identity(),
                             task_ids=[task_id])["tasks"][0]
        sources = row["sources"]
        self.assertEqual(sources["delivery_state"], "prepared_only",
                         "a context prepared for a refused round was never sent")
        self.assertFalse(sources["delivery_authoritative"])
        self.assertEqual(sources["delivered_evidence"], {})
        self.assertEqual(sources["delivery_rounds"], [])
        self.assertNotIn("doc-2", sources["allowed_references"],
                         "the never-sent reference is not a delivered reference")
        self.assertNotIn("doc-1", sources["allowed_references"])
        self.assertEqual(row["metering"]["invocation_attempts"], 1,
                         "the first round's send stays in the call ledger")
        self.assertEqual(row["metering"]["invocation_sent"], ["yes"])

    def test_review_uses_worker_delivery_ledger_not_input_payload(self):
        undelivered = "synthetic-not-in-vault"
        spec = self.spec()
        spec["public_context"]["evidence_summaries"][undelivered] = {
            "summary": "synthetic candidate document", "sha256": "0" * 64}
        self.counter[0] += 1
        task, _ = self.store.create_task(
            title="Delivery ledger check", input_payload=spec, case_id="case-delivery",
            execution_mode="replay", actor="operator")
        result, completed = self.run_full_chain(task)
        self.assertEqual(result["status"], "succeeded")
        delivery = completed["model_call"]["context_delivery"]
        self.assertTrue(any(str(entry).startswith(undelivered + ":")
                            for entry in delivery["dropped_evidence"]),
                        "the worker refused the unverifiable document before egress")
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[task["task_id"]])
        row = review["tasks"][0]
        self.assertEqual(row["sources"]["allowed_references"],
                         sorted(delivery["delivered_evidence"]),
                         "the projection mirrors the worker's delivered set, not the payload")
        self.assertNotIn(undelivered, row["sources"]["allowed_references"])
        self.assertEqual(row["sources"]["delivery_source"], "worker_context_delivery")
        reasons = {item["ref"]: item["reason"]
                   for item in row["sources"]["undelivered_references"]}
        self.assertIn(undelivered, reasons)
        self.assertTrue(reasons[undelivered])
        self.assertEqual(review["attribution"]["independent_evidence_count"], 1,
                         "only actually delivered, vault-verified bytes count")

    def test_provide_input_and_reject_are_not_signoffs(self):
        for decision in ("provide_input", "reject"):
            with self.subTest(decision=decision):
                task = self.create_root(case_id="case-{0}".format(decision))
                task_id = task["task_id"]
                claimed = self.store.claim_task(task_id, "review-worker")
                token = claimed["fencing_token"]
                self.store.mark_running(task_id, worker_id="review-worker",
                                        fencing_token=token,
                                        expected_version=claimed["version"])
                waiting = self.store.request_awaiting_human(
                    task_id, worker_id="review-worker", fencing_token=token,
                    human_request={"question": "synthetic feedback request"})
                self.store.record_human_response(
                    task_id, actor=OWNER, decision=decision,
                    expected_version=waiting["version"],
                    command_id="review-{0}".format(decision),
                    input_patch=({"research_question": "synthetic supplemental input"}
                                 if decision == "provide_input" else None))
                review = project_review(self.store, identity=self.identity(),
                                        task_ids=[task_id])
                row = review["tasks"][0]
                self.assertFalse(row["signoff"]["acknowledged_by_human"])
                self.assertEqual(row["signoff"]["human"], [])
                self.assertEqual(review["attribution"]["human_signoffs"], [])
                self.assertIn(decision, [item["decision"] for item
                                         in row["signoff"]["human_decisions"]],
                              "the decision stays visible without becoming sign-off")

    def test_output_field_alone_is_not_a_signoff(self):
        task = self.create_root(case_id="case-spoofed-signoff")
        task_id = task["task_id"]
        claimed = self.store.claim_task(task_id, "spoof-worker")
        token = claimed["fencing_token"]
        self.store.mark_running(task_id, worker_id="spoof-worker",
                                fencing_token=token,
                                expected_version=claimed["version"])
        self.store.complete(
            task_id, worker_id="spoof-worker", fencing_token=token,
            output={"summary": "synthetic model output",
                    "personal_acknowledgement": {"actor": OWNER}})
        self.assertEqual(
            sum(event["event_type"] == "personal_acknowledgement"
                for event in self.store.events(task_id)), 0)
        review = project_review(self.store, identity=self.identity(),
                                task_ids=[task_id])
        row = review["tasks"][0]
        self.assertFalse(row["signoff"]["acknowledged_by_human"],
                         "task data cannot mint human attribution")
        self.assertEqual(row["signoff"]["human"], [])
        self.assertEqual(review["attribution"]["human_signoffs"], [])
        self.assertEqual(len(row["signoff"]["untrusted_output_claims"]), 1)


# ---------------------------------------------------------------------- #
# 来源与复盘：身份边界、只读、规则候选                                     #
# ---------------------------------------------------------------------- #

class PersonalReviewAccessTests(R22IntegrationCase):
    def personal_task(self, actor=OWNER, question="个人复盘研究问题"):
        created = create_personal_research(
            self.store, actor=actor, question=question, symbols=["600519"],
            execution_mode="replay", config=self.config)
        return created["task"]

    def test_personal_results_are_readable_only_by_their_owner(self):
        task = self.personal_task()
        owner_review = project_review(self.store, identity=self.identity(actor=OWNER),
                                      task_ids=[task["task_id"]])
        self.assertEqual([row["task_id"] for row in owner_review["tasks"]],
                         [task["task_id"]])
        with self.assertRaises(ReviewAccessError) as caught:
            project_review(self.store, identity=self.identity(actor=OTHER),
                           task_ids=[task["task_id"]])
        self.assertEqual(caught.exception.reason, "personal_owner_mismatch")
        with self.assertRaises(ReviewAccessError) as caught:
            project_review(self.store, identity={"credential_id": "anon",
                                                 "scopes": ["read_research"],
                                                 "case_scope": ["*"]},
                           task_ids=[task["task_id"]])
        self.assertEqual(caught.exception.reason, "personal_owner_required")

    def test_shared_results_respect_scope_and_case(self):
        root = self.create_root(case_id="case-shared")
        self.run_full_chain(root)
        project_review(self.store, identity=self.identity(cases=["case-shared"]),
                       task_ids=[root["task_id"]])
        with self.assertRaises(ReviewAccessError) as caught:
            project_review(self.store, identity=self.identity(cases=["case-other"]),
                           task_ids=[root["task_id"]])
        self.assertEqual(caught.exception.reason, "case_out_of_scope")
        with self.assertRaises(ReviewAccessError) as caught:
            project_review(self.store, identity=self.identity(scopes=["create_research"]),
                           task_ids=[root["task_id"]])
        self.assertEqual(caught.exception.reason, "scope_not_granted")

    def test_review_projection_writes_nothing(self):
        root, flow = self.bound_root()
        self.run_role_flow(root, flow)
        count = len(self.store.list_tasks())
        events = len(self.store.events())
        attempts = sum(len(self.store.invocation_attempts(task["task_id"]))
                       for task in self.store.list_tasks())
        max_version = max(task["version"] for task in self.store.list_tasks())
        project_review(self.store, identity=self.identity(),
                       task_ids=[root["task_id"]])
        self.assertEqual(len(self.store.list_tasks()), count)
        self.assertEqual(len(self.store.events()), events)
        self.assertEqual(sum(len(self.store.invocation_attempts(task["task_id"]))
                             for task in self.store.list_tasks()), attempts)
        self.assertEqual(max(task["version"] for task in self.store.list_tasks()),
                         max_version)
        # And a read-only store can serve the same projection.
        readonly = TaskStore(self.db_path, read_only=True)
        self.addCleanup(readonly.close)
        again = project_review(readonly, identity=self.identity(),
                               task_ids=[root["task_id"]])
        self.assertEqual(len(again["tasks"]), 4)


class RuleCandidateTests(R22IntegrationCase):
    def test_rule_change_stays_pending_and_cannot_change_approved_version(self):
        base = self.approved_flow()
        approved_digest = base["flow_sha256"]
        book = RuleCandidateBook()
        candidate = book.propose(
            base, changes={"prompts": {"synthesis_drafter": {
                "version": 2, "text": "综合草稿提示词 v2（待审）"}}, "note": "收紧综合提示词"},
            actor="operator")
        self.assertEqual(candidate["status"], "pending_review")
        self.assertFalse(candidate["applies_to_approved"])
        self.assertEqual(candidate["base"]["flow_sha256"], approved_digest)
        self.assertEqual(candidate["approved_version_unchanged"], approved_digest)
        self.assertNotEqual(candidate["candidate"]["flow_sha256"], approved_digest)
        self.assertEqual(base["flow_sha256"], approved_digest,
                         "the approved artifact object is untouched")
        self.registry.validate(base)  # still the approved version
        with self.assertRaises(RoleFlowError) as caught:
            self.registry.validate(book.candidate_flow(candidate["candidate_id"]))
        self.assertIn("flow_not_registered", str(caught.exception))
        with self.assertRaises(RuleCandidateError) as caught:
            book.approve(candidate["candidate_id"], registry=self.registry,
                         reviewer="event_analyst")
        self.assertIn("reviewer_not_human", str(caught.exception))
        self.assertEqual(book.get(candidate["candidate_id"])["status"], "pending_review")

        approved = book.approve(candidate["candidate_id"], registry=self.registry,
                                reviewer="human-approver")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], "human-approver")
        self.assertEqual(approved["approved_version_unchanged"], approved_digest)
        self.registry.validate(book.candidate_flow(candidate["candidate_id"]))
        self.registry.validate(base)

    def test_propose_does_not_touch_a_registry_or_the_base_flow(self):
        base = make_flow()
        before = json.dumps(base, sort_keys=True, ensure_ascii=False)
        record = propose_rule_change(tree_copy(base), changes={
            "prompts": {"event_analyst": {"version": 2, "text": "事件分析提示词 v2"}}},
            actor="operator")
        self.assertEqual(json.dumps(base, sort_keys=True, ensure_ascii=False), before)
        self.assertEqual(record["status"], "pending_review")
        self.assertEqual(record["base"]["flow_sha256"], base["flow_sha256"])
        with self.assertRaises(RuleCandidateError) as caught:
            propose_rule_change(base, changes={"prompts": {"ghost": {"text": "x"}}},
                                actor="operator")
        self.assertIn("unknown_role", str(caught.exception))
        with self.assertRaises(RuleCandidateError) as caught:
            propose_rule_change(base, changes={"nonsense": True}, actor="operator")
        self.assertIn("malformed_change", str(caught.exception))

    def test_candidate_copy_is_isolated_and_digest_reverified_on_approval(self):
        base = self.approved_flow()
        book = RuleCandidateBook()
        record = book.propose(
            base, changes={"prompts": {"event_analyst": {
                "version": 2, "text": "事件分析提示词 v2（待审）"}}}, actor="event_analyst")
        candidate_id = record["candidate_id"]
        # The reviewer edits the flow they were shown; the frozen candidate
        # must not change, and approval must bind the recorded bytes.
        fetched = book.candidate_flow(candidate_id)
        fetched["prompts"]["event_analyst"]["text"] = "审核后被改写的提示词"
        approved = book.approve(candidate_id, registry=self.registry,
                                reviewer="human-approver")
        self.assertEqual(approved["status"], "approved")
        flow = book.candidate_flow(candidate_id)
        prompt = flow["prompts"]["event_analyst"]
        self.assertEqual(prompt["text"], "事件分析提示词 v2（待审）")
        self.assertEqual(hashlib.sha256(prompt["text"].encode()).hexdigest(),
                         prompt["sha256"],
                         "the approved artifact still carries the recorded digest")
        self.registry.validate(flow)
        self.assertEqual(book.get(candidate_id)["candidate"]["flow_sha256"],
                         flow["flow_sha256"])


def tree_copy(value):
    return json.loads(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
