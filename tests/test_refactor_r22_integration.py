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
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.personal_review import (
    ReviewAccessError,
    actor_kind,
    project_review,
)
from stock_agent.personal_workspace import acknowledge_completed_research, create_personal_research
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
from stock_agent.task_runtime.store import TERMINAL_STATUSES
from stock_agent.task_supply import build_task_spec

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
        self.assertEqual(actor_kind(OWNER), "human")

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


def tree_copy(value):
    return json.loads(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
