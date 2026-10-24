"""R17 (offline slice): controlled role execution — flows, digests, mode
switch, step execution, bounded cycles. The Dify transport half is NOT_RUN
(depends on R16); these tests play the orchestrator exactly where Dify sits
later, driving real components: FlowRegistry artifacts, R22 subtask graph,
the standard worker boundary in replay mode, and the TaskStore audit chain.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.role_execution import (
    FlowRegistry,
    RoleFlowError,
    bind_role_flow,
    define_role_flow,
    execute_role_step,
    finish_role_flow,
    flow_steps,
    prompt_artifact,
    _verify_step_binding,
)
from stock_agent.subtask_graph import cancel_subtree, propose_subtasks
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import STATUS_SUCCEEDED
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

GROUPS = [{"roles": ["event_analyst", "counter_evidence"]},
          {"roles": ["synthesis_drafter"]}]


def make_flow(version=1, synthesis_text="综合草稿提示词 v1"):
    prompts = {
        "event_analyst": prompt_artifact("event_analyst", 1, "事件分析提示词 v1"),
        "counter_evidence": prompt_artifact("counter_evidence", 1, "反证提示词 v1"),
        "synthesis_drafter": prompt_artifact("synthesis_drafter", 1, synthesis_text),
    }
    return define_role_flow(flow_id="event_draft_flow", version=version,
                            groups=GROUPS, prompts=prompts)


def replay_for(summary, confidence=0.4):
    return {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": summary, "key_findings": [], "unknowns": [],
        "evidence_refs": [], "human_questions": [],
        "model_self_assessed_confidence": confidence,
    }


class FlowArtifactTests(unittest.TestCase):
    def test_digest_is_deterministic_and_prompt_sensitive(self):
        first = make_flow()
        again = make_flow()
        self.assertEqual(first["flow_sha256"], again["flow_sha256"],
                         "same artifacts rebuild to the same digest")
        changed = make_flow(synthesis_text="综合草稿提示词 v2 改动")
        self.assertNotEqual(first["flow_sha256"], changed["flow_sha256"],
                            "a prompt text change is a NEW candidate (A77)")

    def test_malformed_flows_refused(self):
        with self.assertRaises(RoleFlowError) as caught:
            define_role_flow(flow_id="", version=1, groups=GROUPS, prompts={})
        self.assertIn("malformed_flow", str(caught.exception))
        # duplicate role across groups
        dup_groups = [{"roles": ["event_analyst"]},
                      {"roles": ["event_analyst", "synthesis_drafter"]}]
        with self.assertRaises(RoleFlowError) as caught:
            define_role_flow(
                flow_id="dup", version=1, groups=dup_groups,
                prompts={"event_analyst": prompt_artifact("event_analyst", 1, "a"),
                         "synthesis_drafter": prompt_artifact("synthesis_drafter", 1, "b")})
        self.assertIn("malformed_flow", str(caught.exception))
        # prompts must cover exactly the flow roles
        with self.assertRaises(RoleFlowError) as caught:
            define_role_flow(flow_id="gap", version=1, groups=GROUPS, prompts={})
        self.assertIn("malformed_flow", str(caught.exception))
        # unknown role
        with self.assertRaises(RoleFlowError) as caught:
            prompt_artifact("rookie_writer", 1, "text")
        self.assertIn("unknown_role", str(caught.exception))

    def test_registry_approvals_bind_digests_not_names(self):
        registry = FlowRegistry()
        v1 = make_flow(version=1)
        with self.assertRaises(RoleFlowError) as caught:
            registry.validate(v1)
        self.assertIn("flow_not_registered", str(caught.exception))
        registry.register(v1)
        with self.assertRaises(RoleFlowError) as caught:
            registry.validate(v1)
        self.assertIn("flow_not_approved", str(caught.exception))
        registry.approve(v1, actor="human-approver")
        registry.validate(v1)  # approved digest covers the artifact
        v2 = make_flow(version=2, synthesis_text="改动后的综合提示词")
        registry.register(v2)
        with self.assertRaises(RoleFlowError) as caught:
            registry.validate(v2)
        # A77: the OLD approval does not carry over to the new candidate
        self.assertIn("approval_digest_changed", str(caught.exception))
        # same version re-registered with a different digest is a conflict
        v1_mutated = make_flow(version=1, synthesis_text="偷偷改提示词")
        with self.assertRaises(RoleFlowError) as caught:
            registry.register(v1_mutated)
        self.assertIn("flow_version_conflict", str(caught.exception))


class R17RoleTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {
            "db_path": str(self.root / "tasks.sqlite3")}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)
        self.counter = [0]
        self.registry = FlowRegistry()

    def make_root(self):
        self.counter[0] += 1
        spec = build_task_spec(
            research_question="角色流研究问题 {0}".format(self.counter[0]),
            symbols=["600519"], execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = {}
        task, _ = self.store.create_task(
            title="role root", input_payload=spec, execution_mode="replay")
        return task

    def bound_root(self, flow=None):
        flow = flow or make_flow()
        self.registry.register(flow)
        self.registry.approve(flow, actor="human-approver")
        root = self.make_root()
        bind_role_flow(self.store, root["task_id"], flow=flow,
                       registry=self.registry, actor="operator")
        return root["task_id"], flow


class BindingTests(R17RoleTestCase):
    def test_bind_is_a_new_audited_input_version(self):
        flow = make_flow()
        self.registry.register(flow)
        with self.assertRaises(RoleFlowError) as caught:
            bind_role_flow(self.store, self.make_root()["task_id"], flow=flow,
                           registry=self.registry, actor="operator")
        self.assertIn("flow_not_approved", str(caught.exception))
        self.registry.approve(flow, actor="human-approver")
        root = self.make_root()
        fresh = bind_role_flow(self.store, root["task_id"], flow=flow,
                               registry=self.registry, actor="operator")
        self.assertEqual(fresh["input_version"], 2, "binding bumps the version")
        snapshots = self.store.input_snapshots(root["task_id"])
        self.assertEqual([s["input_version"] for s in snapshots], [1, 2],
                         "both immutable snapshots stand")
        events = [e["event_type"] for e in self.store.events(root["task_id"])]
        self.assertIn("task_input_bumped", events)
        with self.assertRaises(RoleFlowError) as caught:
            bind_role_flow(self.store, root["task_id"], flow=flow,
                           registry=self.registry, actor="operator")
        self.assertIn("task_not_bound", str(caught.exception))


class MutualExclusionTests(R17RoleTestCase):
    def test_full_chain_never_advances_a_bound_task(self):
        root_id, _flow = self.bound_root()
        plain = self.make_root()  # younger, unbound
        claimed = self.store.claim_next("full-chain-worker", exclude_role_bound=True)
        self.assertEqual(claimed["task_id"], plain["task_id"],
                         "the bound root is skipped by the full-chain claim path")
        # The default claim (orchestrator's door) still sees it — unchanged.
        orchestrator_claim = self.store.claim_next("role-orchestrator")
        self.assertEqual(orchestrator_claim["task_id"], root_id)

        from stock_agent.research_worker import run_research_task

        result = run_research_task(self.store, root_id, worker_id="w",
                                   config=self.config)
        self.assertEqual(result["status"], "refused_role_flow")
        self.assertFalse(result["model_call"]["called"])
        root = self.store.get_task(root_id)
        self.assertEqual(root["status"], "leased",
                         "the full chain advanced nothing — still just the claim")


class StepExecutionTests(R17RoleTestCase):
    def run_step(self, root_id, flow, role, summary):
        return execute_role_step(
            self.store, root_id, role_id=role, flow=flow, registry=self.registry,
            config=self.config,
            step_overrides={"replay_response": replay_for(summary)})

    def test_full_flow_runs_and_finishes_with_provenance(self):
        root_id, flow = self.bound_root()
        self.assertEqual(self.run_step(root_id, flow, "event_analyst", "事件分析完成")["status"],
                         STATUS_SUCCEEDED)
        self.assertEqual(self.run_step(root_id, flow, "counter_evidence", "反证完成")["status"],
                         STATUS_SUCCEEDED)
        with self.assertRaises(RoleFlowError) as caught:
            finish_role_flow(self.store, root_id, flow=flow, registry=self.registry)
        self.assertIn("flow_incomplete", str(caught.exception))
        # synthesis may only start once group 1 is complete — it now is
        self.assertEqual(self.run_step(root_id, flow, "synthesis_drafter", "综合完成")["status"],
                         STATUS_SUCCEEDED)
        report = finish_role_flow(self.store, root_id, flow=flow,
                                  registry=self.registry)
        self.assertEqual(report["status"], STATUS_SUCCEEDED)
        root = self.store.get_task(root_id)
        self.assertEqual(root["status"], STATUS_SUCCEEDED)
        outputs = root["output"]["role_outputs"]
        self.assertEqual(set(outputs),
                         {"event_analyst", "counter_evidence", "synthesis_drafter"})
        self.assertEqual(outputs["event_analyst"]["summary"], "事件分析完成")
        self.assertFalse(root["model_call"]["called"],
                         "the root makes no model call of its own")
        self.assertEqual(len(root["model_call"]["steps"]), 3,
                         "per-step provenance links every output to its task")

    def test_group_ordering_enforced(self):
        root_id, flow = self.bound_root()
        with self.assertRaises(RoleFlowError) as caught:
            self.run_step(root_id, flow, "synthesis_drafter", "跳步")
        self.assertIn("group_not_current", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), 1,
                         "an out-of-order step creates nothing")

    def test_succeeded_step_never_reruns(self):
        root_id, flow = self.bound_root()
        self.run_step(root_id, flow, "event_analyst", "一次就够")
        before = len(self.store.list_tasks())
        with self.assertRaises(RoleFlowError) as caught:
            self.run_step(root_id, flow, "event_analyst", "再来一次计费")
        self.assertIn("step_already_succeeded", str(caught.exception))
        self.assertEqual(len(self.store.list_tasks()), before,
                         "no second subtask, no second billing (A78)")

    def test_failed_step_needs_decision_no_auto_retry(self):
        root_id, flow = self.bound_root()
        # Seed the step directly and terminally fail it (whatever the cause).
        proposal = propose_subtasks(
            self.store, parent_task_id=root_id,
            proposals=[{"role_id": "event_analyst", "question": "失败的步骤"}])
        subtask_id = proposal["created"][0]["task_id"]
        claimed = self.store.claim_next("w", exclude_role_bound=True)
        self.assertEqual(claimed["task_id"], subtask_id)
        self.store.mark_running(subtask_id, worker_id="w",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.fail(subtask_id, worker_id="w",
                        fencing_token=claimed["fencing_token"],
                        error="step failed", retryable=False)
        with self.assertRaises(RoleFlowError) as caught:
            self.run_step(root_id, flow, "event_analyst", "静默重试是不允许的")
        self.assertIn("step_failed_needs_decision", str(caught.exception))
        # The honest remediation is an explicit decision: cancel the subtree.
        report = cancel_subtree(self.store, root_id, actor="operator",
                                reason="step failed")
        self.assertGreaterEqual(report["count"], 1)

    def test_prompt_change_invalidates_old_binding(self):
        root_id, v1 = self.bound_root()
        self.run_step(root_id, v1, "event_analyst", "按 v1 提示词完成")
        v2 = make_flow(version=2, synthesis_text="画布改动后的综合提示词")
        self.registry.register(v2)
        self.registry.approve(v2, actor="human-approver")
        with self.assertRaises(RoleFlowError) as caught:
            self.run_step(root_id, v2, "counter_evidence", "旧绑定跑新流")
        self.assertIn("binding_digest_changed", str(caught.exception))
        # Re-binding the new candidate is an explicit, audited version bump.
        bind_role_flow(self.store, root_id, flow=v2, registry=self.registry,
                       actor="operator")
        self.assertEqual(
            self.run_step(root_id, v2, "counter_evidence", "按新版提示词完成")["status"],
            STATUS_SUCCEEDED)

    def test_tampered_step_prompt_invalidates_step(self):
        flow = make_flow()
        tampered = {
            "task_id": "fake",
            "input_payload": {
                "role_step": {"role_prompt_sha256": flow["prompts"]["event_analyst"]["sha256"]},
                "role_prompt": "被篡改的提示词",
            },
        }
        with self.assertRaises(RoleFlowError) as caught:
            _verify_step_binding(tampered, flow, "event_analyst")
        self.assertIn("step_binding_invalid", str(caught.exception))

    def test_in_progress_step_is_not_duplicated(self):
        root_id, flow = self.bound_root()
        # Seed a step whose replay answer asks a human question: the subtask
        # lands awaiting_human (non-terminal) — a re-invocation must observe
        # it rather than spawn a twin.
        asking = dict(replay_for("待人工"))
        asking["human_questions"] = [{
            "question": "补充口径?", "why_needed": "角色步骤需要人工输入",
            "options": ["补充", "跳过"]}]
        first = execute_role_step(
            self.store, root_id, role_id="event_analyst", flow=flow,
            registry=self.registry, config=self.config,
            step_overrides={"replay_response": asking})
        self.assertEqual(first["status"], "awaiting_human")
        again = execute_role_step(
            self.store, root_id, role_id="event_analyst", flow=flow,
            registry=self.registry, config=self.config)
        self.assertEqual(again["status"], "step_in_progress")
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertEqual(len(flow_steps(self.store, root_id)), 1)


if __name__ == "__main__":
    unittest.main()
