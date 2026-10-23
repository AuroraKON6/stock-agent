"""R15: W10 first personal research — REPLAY SKELETON (end-to-end).

Runs the ENTIRE blueprint vertical slice through the real production
components, in replay mode (no real model call — honestly NOT_RUN for the
live-model half), over an EXPLICITLY-LABELLED fixture announcement in the
R12a personal namespace:

    login(session) → workspace create → personal evidence import
    → HTML body excerpt (R12a) → typed fact (R05) → approved fact bundle
    (R13) → replay worker run → user follow-up (A64) → v2 re-run
    → explicit re-research (A90) → personal acknowledgement (A50).

Every stage leaves its durable artifact (task events, input snapshots, fact
bundle hash, namespace audit) and the test asserts the chain end to end.
The REAL-event materials and the REAL human sign-off remain gated: they are
the user's part of W10 (A69 — an agent never fills them in).
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_excerpts import excerpt_selector
from stock_agent.identity_providers import PersonalSessionStore
from stock_agent.personal_evidence import (
    import_personal_document,
    personal_import_audit,
    personal_vault_view,
)
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

OWNER = "lead-analyst"
# W10-SKELETON fixture announcement — NOT a real filing. The real W10 run
# swaps this for an actual official disclosure (file + exact URL) supplied
# by the user; nothing in the chain depends on the fixture being real.
SKELETON_ANNOUNCEMENT = """<!DOCTYPE html><html><head><title>W10骨架样本公告</title></head>
<body><h1>样本公司 2025 年第三季度报告</h1>
<p>本公告为 W10 replay 骨架的明示样本文本，不是任何真实公司的披露文件。</p>
<p>营业收入为 1,234.56 万元，同比 -7.89%。</p>
<p>研发费用为（300.00）万元。</p>
</body></html>"""


class W10SkeletonTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

        def authenticator(config, actor, symbols):
            if actor != OWNER:
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-w10-skeleton", "policy_sha256": "f" * 64}

        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=PersonalSessionStore(ttl_seconds=1800),
            login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.cookie = self._login()

    # ---------------- HTTP helpers ---------------- #

    def _post(self, path, body, *, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json",
                       "X-Task-Board-CSRF": str(self.server.csrf_token),
                       "Cookie": cookie if cookie is not None else self.cookie}
            conn.request("POST", path, json.dumps(body), headers)
            response = conn.getresponse()
            data = response.read().decode("utf-8")
            try:
                return response.status, json.loads(data)
            except json.JSONDecodeError:
                return response.status, {"raw": data}
        finally:
            conn.close()

    def _login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/v1/personal/login",
                         json.dumps({"actor": OWNER, "symbols": ["600519"]}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            return (response.getheader("Set-Cookie") or "").split(";")[0]
        finally:
            conn.close()

    def _run_task(self, task_id, config=None):
        claimed = self.store.claim_next("w-w10")
        self.assertEqual(claimed["task_id"], task_id)
        from stock_agent.research_worker import run_research_task

        return run_research_task(self.store, task_id, worker_id="w-w10",
                                 config=config or self.config)

    # ---------------- the vertical slice ---------------- #

    def test_w10_replay_skeleton_full_chain(self):
        # 1) Personal research created through the workspace route.
        status, created = self._post("/api/v1/workspace/create", {
            "question": "样本公司三季度业绩变动的驱动因素？", "symbols": ["600519"]})
        self.assertEqual(status, 200)
        task_id = created["task_id"]
        self.assertEqual(created["outcome"], "RECORDED")

        # 2) Fixture announcement imported into the OWNER's personal namespace
        #    (R12a) with the policy audit stamp.
        source = self.root / "w10_incoming"
        source.mkdir(parents=True, exist_ok=True)
        path = source / "announcement.html"
        path.write_text(SKELETON_ANNOUNCEMENT, encoding="utf-8")
        imported = import_personal_document(
            self.config, OWNER, str(path),
            source_url="https://www.cninfo.com.cn/new/disclosure/detail?plate=szse&fixture=w10-skeleton",
            title="W10骨架样本公告（非真实披露）",
            published_at="2026-10-23T18:00:00+08:00")
        document = imported["document"]
        audit = personal_import_audit(self.config, OWNER)
        self.assertEqual(audit[-1]["institutional_admission"], "NOT_REQUESTED")

        # 3) Body → located excerpt (R12a) → typed fact (R05), all inside the
        #    personal namespace view.
        view = personal_vault_view(self.config, OWNER)
        from stock_agent.evidence_excerpts import _document_text, create_excerpt

        text, version = _document_text(view, document)
        self.assertEqual(version, "body-extract-v2-html+utf8-codepoints-v1")
        start = text.index("营业收入为")
        end = text.index("。", start) + 1
        excerpt = create_excerpt(view, document["document_id"], start, end)
        from stock_agent.evidence_facts import build_fact

        fact = build_fact(view, excerpt_selector(excerpt), fact_type="statement",
                          statement="营业收入为 1,234.56 万元。", period="2025Q3")

        # 4) The fact rides the task payload as an approved fact selector;
        #    run the replay worker (R06 quality + R13 bundle ride along).
        task = self.store.get_task(task_id)
        payload = dict(task["input_payload"])
        payload["approved_facts"] = [excerpt_selector(excerpt)]
        payload["public_context"]["evidence_summaries"] = {
            document["document_id"]: {"summary": "样本公告正文（骨架）",
                                      "sha256": document["sha256"]}}
        # Replace the task under the same intent with the enriched payload.
        self.store._conn.execute(
            "UPDATE research_tasks SET input_payload = ? WHERE task_id = ?",
            (json.dumps(payload, ensure_ascii=False), task_id))
        self.store._conn.commit()
        result = self._run_task(task_id, config=view)
        # Replay default response asks a human question → awaiting.
        self.assertEqual(result["status"], "awaiting_human")
        stored = self.store.get_task(task_id)
        model_call = stored["model_call"]
        bundle = model_call.get("fact_bundle") or {}
        self.assertTrue(bundle.get("bundle_sha256"),
                       "the approved fact reached the attempt as a frozen bundle")
        self.assertIn("1,234.56", (model_call.get("outbound_context") or {})
                      .get("verified_facts", [{}])[0].get("content", ""))

        # 5) User follow-up (A64): input version bumps, snapshot preserved.
        awaiting = self.store.get_task(task_id)
        status, followed = self._post("/api/v1/workspace/followup", {
            "task_id": task_id, "expected_version": awaiting["version"],
            "extra_note": "追问：剔除研发费用后的口径如何？"})
        self.assertEqual(status, 200)
        self.assertEqual(followed["task"]["input_version"], awaiting["input_version"] + 1)
        snapshots = self.store.input_snapshots(task_id)
        self.assertEqual(len(snapshots), 2, "v1 and v2 inputs both preserved")

        # 6) v2 re-run completes (no question this time → direct terminal).
        payload2 = dict(json.loads(json.dumps(followed["task"]["input_payload"])))
        payload2["replay_response"]["human_questions"] = []
        self.store._conn.execute(
            "UPDATE research_tasks SET input_payload = ? WHERE task_id = ?",
            (json.dumps(payload2, ensure_ascii=False), task_id))
        self.store._conn.commit()
        result2 = self._run_task(task_id)
        self.assertEqual(result2["status"], "succeeded",
                         "v2 (follow-up input) run reaches a terminal state")

        # 7) Explicit re-research (A90): new intent, prior linked, old intact.
        status, rerun = self._post("/api/v1/workspace/rerun", {"task_id": task_id})
        self.assertEqual(status, 200)
        origin = self.store.get_task(rerun["task_id"])["input_payload"]["origin"]
        self.assertEqual(origin["prior_task_id"], task_id)
        self.assertEqual(self.store.get_task(task_id)["status"], "succeeded")

        # 8) Personal acknowledgement (A50): scoped, institutional keys absent.
        rerun_id = rerun["task_id"]
        self._run_task(rerun_id)
        awaiting2 = self.store.get_task(rerun_id)
        if awaiting2["status"] == "awaiting_human":
            acknowledged = self.store.record_human_response(
                rerun_id, actor=OWNER, decision="acknowledge",
                expected_version=awaiting2["version"], command_id="w10-ack")
        else:
            acknowledged = awaiting2
        output = acknowledged["output"]
        if "personal_acknowledgement" in output:
            self.assertEqual(output["personal_acknowledgement"]["scope"],
                             "personal_research_acknowledgement_not_institutional_approval")
        for forbidden in ("institutional_approval", "quality_approval"):
            self.assertNotIn(forbidden, output)

        # 9) The whole chain is auditable from durable state.
        events = [e["event_type"] for e in self.store.events(task_id)]
        for expected in ("task_created", "task_claimed", "human_requested",
                         "human_response_recorded", "task_succeeded"):
            self.assertIn(expected, events)
        self.assertTrue(bundle["bundle_sha256"])
        self.assertEqual(audit[-1]["owner"], OWNER)


if __name__ == "__main__":
    unittest.main()
