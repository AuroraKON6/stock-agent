"""R15: personal announcement facts from the NORMAL entries into the model
request and the report — local replay evidence (KONM-23, round 1).

Unlike ``test_refactor_r15_w10_skeleton.py`` — which writes
``approved_facts`` back into the test DB's input_payload and therefore cannot
prove a user-created input carries the same facts — every write here goes
through a production entry:

    personal login → ``import_personal_document`` (R12a) →
    ``link_evidence_to_signal`` association (the vault's own reviewed API,
    CLI-exposed) → ``create_excerpt`` + ``build_fact`` (R12a/R05) →
    ``POST /api/v1/workspace/create`` with ``document_ids`` +
    ``fact_selectors`` (the same route the page uses) → normal worker
    attempt (claim + ``run_research_task``) against the owner's personal
    vault view.

The assertions read the model-request construction boundary and the
persisted records only — no fact is injected into the task table, and
material that merely sits in the input history is never counted as
delivered. The announcement is an EXPLICITLY-labelled fixture; the live
model half is honestly NOT_RUN (the live-round test stubs the single
outbound call at the pi boundary and asserts on the constructed request).

Negative controls: an unassociated control document stays in the input
history but the delivery record shows it dropped; a tampered or
foreign-namespace selector is rejected at create with the queue untouched.
"""
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_excerpts import (
    _document_text,
    create_excerpt,
    excerpt_selector,
)
from stock_agent.evidence_facts import build_fact
from stock_agent.identity_providers import PersonalSessionStore
from stock_agent.models import AnalyzedSignal, now_iso
from stock_agent.personal_evidence import (
    import_personal_document,
    personal_vault_view,
)
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import enforced_governance, vault_config

OWNER = "lead-analyst"
FOREIGN_OWNER = "another-analyst"
FIXTURE_SIGNAL_ID = "sig-r15-fixture-filing"

# R15 round-1 fixture announcements — NOT real filings. The real W10 case
# swaps these for an official disclosure (file + exact URL) supplied by the
# user; nothing in the chain depends on the fixture being real.
ANNOUNCEMENT_A = """<!DOCTYPE html><html><head><title>R15样本公告甲</title></head>
<body><h1>样本公司 2026 年第三季度样本报告</h1>
<p>本公告为 R15 个人事实链路验证的明示样本文本，不是任何真实公司的披露文件。</p>
<p>本季度样本营业收入为 2,345.67 万元，同比 +12.34%。</p>
<p>样本研发费用为（456.78）万元。</p>
</body></html>"""
ANNOUNCEMENT_B = """<!DOCTYPE html><html><head><title>R15对照公告乙</title></head>
<body><h1>对照样本公司 2026 年第三季度样本报告</h1>
<p>本公告为 R15 未关联对照的明示样本文本，不是任何真实公司的披露文件。</p>
<p>对照样本净利润为 999.99 万元。</p>
</body></html>"""
FACT_SENTENCE_HEAD = "本季度样本营业收入为"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class R15PersonalFactsTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Real path (resolved): on macOS the raw temp dir sits behind the
        # /var symlink, which the vault's reparse-point guard rejects.
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        # Handler threads do not inherit the contextvar override (same
        # production pattern as tests/personal_board_entry.py): the
        # module-level root applies process-wide so the board server's
        # personal-namespace lookups land in the temp workspace too.
        import stock_agent.config as config_module

        original_root = config_module.PROJECT_ROOT
        config_module.PROJECT_ROOT = self.root
        self.addCleanup(setattr, config_module, "PROJECT_ROOT", original_root)
        self.config = enforced_governance(vault_config(self.root, {}), self.root)
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        # One fixture filing signal in the shared signals store: the plan
        # scope a research task binds. Seeded through the production store;
        # public classification, no synthetic/replay provenance marker.
        from stock_agent.storage import SignalStore

        signals = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            signals.upsert_many([AnalyzedSignal(
                signal_id=FIXTURE_SIGNAL_ID, source="manual", source_type="filing",
                title="R15 fixture 样本公告信号", summary="fixture filing signal",
                url="https://www.cninfo.com.cn/new/disclosure/detail?plate=szse&fixture=r15",
                published_at=now_iso(), collected_at=now_iso(), symbols=["600519"],
                payload={"data_classification": "public"},
            )])
        finally:
            signals.close()
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

        def authenticator(config, actor, symbols):
            if actor not in (OWNER, FOREIGN_OWNER):
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-r15-personal-facts", "policy_sha256": "c" * 64}

        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=PersonalSessionStore(ttl_seconds=1800),
            login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.cookie = self._login(OWNER)

    # ---------------- HTTP helpers ---------------- #

    def _request(self, method, path, body=None, *, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json",
                       "X-Task-Board-CSRF": str(self.server.csrf_token),
                       "Cookie": cookie if cookie is not None else self.cookie}
            conn.request(method, path,
                         json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            data = response.read().decode("utf-8")
            try:
                return response.status, json.loads(data)
            except json.JSONDecodeError:
                return response.status, {"raw": data}
        finally:
            conn.close()

    def _login(self, actor):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/v1/personal/login",
                         json.dumps({"actor": actor, "symbols": ["600519"]}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            return (response.getheader("Set-Cookie") or "").split(";")[0]
        finally:
            conn.close()

    def _create_research(self, **extra):
        body = {"question": "样本公司三季度营业收入公告事实核对？",
                "symbols": ["600519"]}
        body.update(extra)
        return self._request("POST", "/api/v1/workspace/create", body)

    # ---------------- evidence helpers (production entries) ---------------- #

    def _import(self, owner, html, name, title):
        source = self.root / "r15_incoming_{0}".format(owner)
        source.mkdir(parents=True, exist_ok=True)
        path = source / name
        path.write_text(html, encoding="utf-8")
        imported = import_personal_document(
            self.config, owner, str(path),
            source_url=("https://www.cninfo.com.cn/new/disclosure/detail"
                        "?plate=szse&fixture=r15-{0}".format(name)),
            title=title, published_at="2026-11-20T18:00:00+08:00")
        return imported["document"]

    def _prepare_owner_evidence(self):
        """The user's own actions through production APIs: import A+B, link A
        to the plan signal, excerpt the sample sentence, build the typed
        fact. Returns everything the assertions need to tie together."""
        document_a = self._import(
            OWNER, ANNOUNCEMENT_A, "announcement_a.html",
            "R15样本公告甲（fixture，非真实披露）")
        document_b = self._import(
            OWNER, ANNOUNCEMENT_B, "announcement_b.html",
            "R15对照公告乙（fixture，非真实披露）")
        view = personal_vault_view(self.config, OWNER)
        # Plan-allowed association (the reviewed vault API, CLI-exposed):
        # document A joins the fixture signal this task's scope binds.
        from stock_agent.evidence_vault import link_evidence_to_signal

        link_evidence_to_signal(
            view, document_id=document_a["document_id"],
            signal_id=FIXTURE_SIGNAL_ID, actor=OWNER,
            actor_role="information_analyst",
            notes="R15 fixture: 样本公告甲关联计划信号")
        text, extractor = _document_text(view, document_a)
        self.assertEqual(extractor, "body-extract-v2-html+utf8-codepoints-v1")
        start = text.index(FACT_SENTENCE_HEAD)
        end = text.index("。", start) + 1
        excerpt = create_excerpt(view, document_a["document_id"], start, end)
        selector = excerpt_selector(excerpt)
        fact = build_fact(view, selector, fact_type="statement",
                          statement="本季度样本营业收入为 2,345.67 万元。",
                          period="2026Q3")
        return {"document_a": document_a, "document_b": document_b,
                "view": view, "excerpt": excerpt, "selector": selector,
                "fact": fact}

    def _install_replay_answer(self, task_id, document_id):
        """Fixture prep through the GOVERNED input-version bump (auditable,
        never raw SQL — same pattern as tests/personal_board_entry.py): the
        recorded replay answer cites the announcement document. Facts and
        attachments from v1 carry into v2 unchanged."""
        replay = {
            "schema_version": "1.0", "task_id": "placeholder",
            "summary": "REPLAY 草稿：基于所附样本公告事实核对营业收入，未调用外部模型。",
            "key_findings": [{
                "text": "样本公告正文披露：本季度样本营业收入为 2,345.67 万元。",
                "evidence_refs": [document_id]}],
            "unknowns": ["replay 模式未做真实分析"],
            "evidence_refs": [document_id],
            "human_questions": [],
            "model_self_assessed_confidence": 0.4,
        }
        return self.store.bump_input_version(
            task_id, payload_overrides={"replay_response": replay},
            actor="fixture-replay",
            reason="fixture: replay answer citing the sample announcement")

    def _run_replay_worker(self, task_id, view):
        claimed = self.store.claim_next("w-r15-facts")
        self.assertEqual(claimed["task_id"], task_id)
        from stock_agent.research_worker import run_research_task

        return run_research_task(self.store, task_id, worker_id="w-r15-facts",
                                 config=view)

    def _run_production_entry(self, task_id):
        """The production claim path: the CLI ``task-run-next`` handler with
        the APP config only (no hand-passed owner view) — the worker must
        resolve the personal namespace from its own trusted records."""
        from stock_agent.cli import _task_run_next

        config = dict(self.config, task_runtime={"db_path": str(self.store.db_path)})
        config_file = self.root / "worker-config.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        _task_run_next(str(config_file), "w-r15-cli", 120)
        return self.store.get_task(task_id)

    # ---------------- the chain ---------------- #

    def test_replay_chain_from_normal_entries(self):
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        document_b = evidence["document_b"]
        selector = evidence["selector"]

        # 1) Research created through the workspace route with BOTH the
        #    document selection and the approved fact selector.
        status, created = self._create_research(
            document_ids=[document_a["document_id"], document_b["document_id"]],
            fact_selectors=[selector])
        self.assertEqual(status, 200)
        self.assertEqual(created["outcome"], "RECORDED")
        self.assertEqual(created["documents_attached"], 2)
        self.assertEqual(created["facts_attached"], 1)
        task_id = created["task_id"]

        # 2) The FIRST input version already carries the complete contract:
        #    the selector byte-identical to what the user approved, both
        #    document digests, and the plan-signal binding.
        task = self.store.get_task(task_id)
        payload = task["input_payload"]
        self.assertEqual(payload["approved_facts"], [selector])
        summaries = payload["public_context"]["evidence_summaries"]
        self.assertEqual(
            {ref: item["sha256"] for ref, item in summaries.items()},
            {document_a["document_id"]: document_a["sha256"],
             document_b["document_id"]: document_b["sha256"]})
        self.assertIn(FIXTURE_SIGNAL_ID, payload["bound_signal_ids"])
        self.assertEqual(len(self.store.input_snapshots(task_id)), 1,
                         "v1 snapshot written at creation")
        # The typed fact itself stays in the user's local review space; only
        # the SELECTOR rides the task (content stays in the vault).
        self.assertNotIn(evidence["fact"]["fact_id"], json.dumps(payload))

        # 3) Fixture replay answer installed via the governed version bump.
        self._install_replay_answer(task_id, document_a["document_id"])
        self.assertEqual(self.store.get_task(task_id)["input_version"], 2)

        # 4) One NORMAL worker attempt against the owner's personal view.
        result = self._run_replay_worker(task_id, evidence["view"])
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        stored = self.store.get_task(task_id)

        # 5) Item-by-item mapping, read from persisted records only.
        model_call = stored["model_call"]
        bundle = model_call["fact_bundle"]
        self.assertEqual(bundle["task_id"], task_id)
        self.assertEqual(bundle["input_version"], 2)
        delivery = model_call["context_delivery"]
        # Original digest: only the ASSOCIATED document delivers; the
        # control document sits in the input history but is honestly
        # reported as not delivered.
        self.assertEqual(delivery["delivered_evidence"], [document_a["document_id"]])
        self.assertIn("{0}:not_associated_with_task".format(document_b["document_id"]),
                      delivery["dropped_evidence"])
        self.assertIn(FIXTURE_SIGNAL_ID, delivery["signal_binding"]["verified_signal_ids"])
        outbound = model_call["outbound_context"]
        delivered_a = outbound["evidence_summaries"][document_a["document_id"]]
        self.assertEqual(delivered_a["sha256"], document_a["sha256"])
        self.assertTrue(delivered_a["vault_verified"])
        self.assertEqual(delivered_a["summary_source"], "vault_projection")
        self.assertNotIn(document_b["document_id"], outbound["evidence_summaries"])
        # Fact selector → bundle fact: same excerpt, same document, locator.
        bundle_fact = bundle["facts"][0]
        excerpt = evidence["excerpt"]
        self.assertEqual(bundle_fact["fact_key"], excerpt["excerpt_id"])
        self.assertEqual(bundle_fact["document_sha256"], document_a["sha256"])
        self.assertEqual(bundle_fact["locator"]["start"], selector["start"])
        self.assertEqual(bundle_fact["locator"]["end"], selector["end"])
        self.assertEqual(bundle_fact["locator"]["extractor_version"],
                         selector["extractor_version"])
        self.assertEqual(bundle_fact["egress_basis"], "verified_excerpt_host_policy")
        self.assertIn(FACT_SENTENCE_HEAD, bundle_fact["content"])
        # Bundle digest + A63 payload-hash binding.
        self.assertTrue(bundle["bundle_sha256"])
        self.assertEqual(bundle["outbound_payload_sha256"], delivery["outbound_sha256"])
        self.assertEqual(bundle["evidence_documents"],
                         [{"sha256": document_a["sha256"],
                           "document_id": document_a["document_id"]}])
        # Model-request content: the minimized context the request is built
        # from carries the same fact content, bounded to 1000 chars.
        facts = outbound["verified_facts"]
        self.assertEqual(len(facts), 1)
        self.assertIn(FACT_SENTENCE_HEAD, facts[0]["content"])
        self.assertIn("2,345.67", facts[0]["content"])
        # Replay report: citations trace back to the announcement.
        output = stored["output"]
        self.assertEqual(output["evidence_refs"], [document_a["document_id"]])
        self.assertEqual(output["key_findings"][0]["evidence_refs"],
                         [document_a["document_id"]])
        self.assertEqual(output["quality"]["verdict"], "ok",
                         "the cited numbers are verifiable in the delivered facts")
        self.assertFalse(output["quality"]["unsupported_specific_claims"])
        # Durable audit trail for the whole chain.
        events = [event["event_type"] for event in self.store.events(task_id)]
        for expected in ("task_created", "task_input_bumped", "task_claimed",
                         "task_succeeded"):
            self.assertIn(expected, events)

        # 6) The read-side surface reports the same honest delivery state.
        status, detail = self._request("GET", "/api/v1/tasks/{0}".format(task_id))
        self.assertEqual(status, 200)
        self.assertEqual(detail["evidence_delivery"]["delivered_evidence"],
                         [document_a["document_id"]])
        self.assertIn("{0}:not_associated_with_task".format(document_b["document_id"]),
                      detail["evidence_delivery"]["dropped_evidence"])
        self.assertEqual(detail["bound_signal_ids"], [FIXTURE_SIGNAL_ID])

    def test_live_prompt_boundary_carries_the_same_fact(self):
        """The live half's construction boundary: with the single outbound
        call stubbed at the pi runtime, the user_content actually built for
        the model embeds the SAME fact content, and its public_context hashes
        to the delivery record's outbound_sha256 (A63)."""
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        status, created = self._create_research(
            execution_mode="live",
            document_ids=[document_a["document_id"]],
            fact_selectors=[evidence["selector"]])
        self.assertEqual(status, 200)
        task_id = created["task_id"]
        task = self.store.get_task(task_id)
        self.assertEqual(task["execution_mode"], "live")

        final = {
            "schema_version": "1.0", "task_id": task_id,
            "summary": "基于样本公告事实的结论。",
            "key_findings": [{
                "text": "样本公告正文披露：本季度样本营业收入为 2,345.67 万元。",
                "evidence_refs": [document_a["document_id"]]}],
            "unknowns": [], "evidence_refs": [document_a["document_id"]],
            "human_questions": [], "model_self_assessed_confidence": 0.5,
        }
        from stock_agent.model_operations import LLMResponse

        claimed = self.store.claim_next("w-r15-live")
        self.assertEqual(claimed["task_id"], task_id)
        from stock_agent.research_worker import run_research_task

        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=LLMResponse(
                       json.dumps(final, ensure_ascii=False),
                       {"model": "deepseek-flash", "request_id": "req-r15",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
                   ) as stub:
            result = run_research_task(self.store, task_id, worker_id="w-r15-live",
                                       config=evidence["view"])
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(stub.call_count, 1)
        user_content = stub.call_args.kwargs.get("user_content") or stub.call_args[0][1]

        # The request the worker constructed: task identity + the SAME fact.
        prompt = json.loads(user_content)
        self.assertEqual(prompt["task_id"], task_id)
        self.assertEqual(prompt["input_version"], 1)
        facts = prompt["public_context"]["verified_facts"]
        self.assertEqual(len(facts), 1)
        self.assertIn(FACT_SENTENCE_HEAD, facts[0]["content"])
        self.assertIn("2,345.67", facts[0]["content"])
        self.assertIn(document_a["document_id"], prompt["public_context"]["evidence_summaries"])
        # A63 at the request boundary: the context embedded in the prompt is
        # exactly the approved payload the delivery record froze.
        from stock_agent.fact_bundle import payload_digest

        stored = self.store.get_task(task_id)
        self.assertEqual(payload_digest(prompt["public_context"]),
                         stored["model_call"]["context_delivery"]["outbound_sha256"])
        # The persisted attempt records the exact prompt hash.
        round_record = stored["model_call"]["rounds"][0]
        self.assertEqual(round_record["prompt_sha256"], _sha256(user_content))
        self.assertEqual(stored["model_call"]["called"], True)
        self.assertEqual(stored["output"]["evidence_refs"],
                         [document_a["document_id"]])

    # ---------------- negative controls ---------------- #

    def test_production_claim_entry_resolves_personal_evidence(self):
        """The planner's rework check, kept as a regression: the production
        CLI claim handler passes ONLY the app config — the worker resolves
        the input owner's personal namespace from the store's own v1
        snapshot record, and the shared signals store still binds the task."""
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        status, created = self._create_research(
            document_ids=[document_a["document_id"]],
            fact_selectors=[evidence["selector"]])
        self.assertEqual(status, 200)
        task_id = created["task_id"]
        self._install_replay_answer(task_id, document_a["document_id"])
        stored = self._run_production_entry(task_id)
        self.assertEqual(stored["status"], "succeeded", stored.get("last_error"))
        model_call = stored["model_call"]
        self.assertIn(document_a["document_id"],
                      model_call["context_delivery"]["delivered_evidence"])
        self.assertEqual(len(model_call["fact_bundle"]["facts"]), 1)
        self.assertEqual(model_call["fact_bundle"]["excluded"], [])
        self.assertEqual(stored["output"]["evidence_refs"],
                         [document_a["document_id"]])

    def test_unassociated_document_excerpt_excluded_consistently(self):
        """A fact selector on a document WITHOUT the task-signal association
        is excluded with the SAME rule as its evidence summary — in the
        request, the fact bundle audit, and the case detail."""
        evidence = self._prepare_owner_evidence()
        document_b = evidence["document_b"]  # imported, never linked
        view = personal_vault_view(self.config, OWNER)
        text, _ = _document_text(view, document_b)
        start = text.index("对照样本净利润")
        excerpt = create_excerpt(view, document_b["document_id"],
                                 start, text.index("。", start) + 1)
        status, created = self._create_research(
            document_ids=[document_b["document_id"]],
            fact_selectors=[excerpt_selector(excerpt)])
        self.assertEqual(status, 200)
        task_id = created["task_id"]
        self._install_replay_answer_no_citation(task_id)
        result = self._run_replay_worker(task_id, view)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        stored = self.store.get_task(task_id)
        model_call = stored["model_call"]
        delivery = model_call["context_delivery"]
        # The summary and the excerpt tell the SAME story: not associated.
        self.assertEqual(delivery["delivered_evidence"], [])
        self.assertIn("{0}:not_associated_with_task".format(document_b["document_id"]),
                      delivery["dropped_evidence"])
        bundle = model_call["fact_bundle"]
        self.assertEqual(bundle["facts"], [])
        reasons = {item.get("reason") for item in bundle["excluded"]}
        self.assertIn("not_associated_with_task", reasons)
        self.assertEqual(bundle["evidence_documents"], [])
        # The request carries no fact content from the undelivered document.
        self.assertNotIn("verified_facts", model_call["outbound_context"])
        self.assertNotIn("999.99", json.dumps(model_call["outbound_context"]))
        # The case detail reads back the same verdict on all three surfaces.
        status, detail = self._request("GET", "/api/v1/tasks/{0}".format(task_id))
        self.assertEqual(status, 200)
        self.assertEqual(detail["evidence_delivery"]["delivered_evidence"], [])
        self.assertIn("{0}:not_associated_with_task".format(document_b["document_id"]),
                      detail["evidence_delivery"]["dropped_evidence"])
        detail_bundle = detail["model_call"]["fact_bundle"]
        self.assertEqual(detail_bundle["delivered_facts"], [])
        self.assertIn("not_associated_with_task",
                      {item.get("reason") for item in detail_bundle["excluded"]})
        self.assertNotIn("verified_facts", detail["outbound_context"])

    def _install_replay_answer_no_citation(self, task_id):
        """A replay answer that cites nothing (nothing was deliverable)."""
        replay = {
            "schema_version": "1.0", "task_id": "placeholder",
            "summary": "REPLAY 草稿：所附原件未关联任务信号，无可引用事实。",
            "key_findings": [], "unknowns": ["原件未送达，无法核对"],
            "evidence_refs": [], "human_questions": [],
            "model_self_assessed_confidence": 0.3,
        }
        return self.store.bump_input_version(
            task_id, payload_overrides={"replay_response": replay},
            actor="fixture-replay",
            reason="fixture: replay answer without citations")

    def test_foreign_namespace_selector_alone_rejected(self):
        """Selector ownership in isolation: a selector minted in ANOTHER
        owner's namespace is rejected even WITHOUT any document selection,
        so the earlier unknown_document shortcut never masked it."""
        foreign = self._import(
            FOREIGN_OWNER, ANNOUNCEMENT_A, "foreign.html",
            "他人命名空间样本公告（fixture）")
        foreign_view = personal_vault_view(self.config, FOREIGN_OWNER)
        text, _ = _document_text(foreign_view, foreign)
        start = text.index(FACT_SENTENCE_HEAD)
        excerpt = create_excerpt(foreign_view, foreign["document_id"],
                                 start, text.index("。", start) + 1)
        status, rejected = self._create_research(
            fact_selectors=[excerpt_selector(excerpt)])
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"], "invalid_fact_selector",
                         "the selector alone must fail ownership, not via documents")
        self.assertIsNone(self.store.claim_next("w-r15-facts"))

    def test_accepted_selector_carries_a_citable_source(self):
        """Excerpt-only submission (no document_ids): every accepted fact
        rides WITH its source document — the create attaches the selector's
        document summary, so the delivered context has a citable original
        and a report citing it succeeds through the production entry."""
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        status, created = self._create_research(
            execution_mode="live",
            fact_selectors=[evidence["selector"]])
        self.assertEqual(status, 200)
        self.assertEqual(created["facts_attached"], 1)
        task_id = created["task_id"]
        # v1 already carries the implied source attachment.
        payload = self.store.get_task(task_id)["input_payload"]
        self.assertEqual(
            payload["public_context"]["evidence_summaries"],
            {document_a["document_id"]: {"summary": "R15样本公告甲（fixture，非真实披露）",
                                         "sha256": document_a["sha256"]}})
        response = {
            "schema_version": "1.0", "task_id": task_id,
            "summary": "Fixture citation check.",
            "key_findings": [{"text": "样本营业收入为 2,345.67 万元。",
                              "evidence_refs": [document_a["document_id"]]}],
            "unknowns": [], "evidence_refs": [document_a["document_id"]],
            "human_questions": [], "model_self_assessed_confidence": 0.5,
        }
        from stock_agent.model_operations import LLMResponse

        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=LLMResponse(
                       json.dumps(response, ensure_ascii=False),
                       {"model": "deepseek-flash", "request_id": "req-r15-cite",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})) as stub:
            stored = self._run_production_entry(task_id)
        stub.assert_called_once()
        self.assertEqual(stored["status"], "succeeded", stored.get("last_error"))
        request = json.loads(stub.call_args.kwargs["user_content"])
        context = request["public_context"]
        self.assertIn(document_a["document_id"], context["evidence_summaries"])
        self.assertEqual(len(context["verified_facts"]), 1)
        self.assertEqual(stored["output"]["evidence_refs"],
                         [document_a["document_id"]])
        stored2 = self.store.get_task(task_id)
        self.assertEqual(stored2["model_call"]["context_delivery"]["delivered_evidence"],
                         [document_a["document_id"]])

    def test_same_name_non_personal_task_uses_shared_view_only(self):
        """Ownership binding: the personal vault view joins ONLY for tasks
        the personal entry created. A generic-intake task recording the SAME
        audit actor name — even when its v1 BUSINESS INPUT forges the
        personal-entry origin stamp, and a governed bump later claims the
        personal document and selector — has NO server-side binding row, so
        the worker resolves the SHARED view only: the material drops as not
        verified, never the owner's namespace."""
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        from stock_agent.personal_evidence import PERSONAL_ORIGIN_VIA
        from stock_agent.task_intake import ensure_question_task

        created = ensure_question_task(
            self.store, research_question="同名非个人任务：管线口径建研？",
            symbols=["600519"], config=self.config, actor=OWNER,
            # The generic intake accepts caller business input — the v1
            # snapshot will faithfully record this forged stamp. The worker
            # must not treat it as ownership: no binding row exists.
            spec_overrides={"origin": {"created_via": PERSONAL_ORIGIN_VIA}})
        task_id = created["task"]["task_id"]
        self.assertIsNone(self.store.input_owner_binding(task_id),
                          "generic business input can never write the binding")
        v1 = next(s for s in self.store.input_snapshots(task_id)
                  if s["input_version"] == 1)
        self.assertEqual(v1["payload"]["origin"]["created_via"], PERSONAL_ORIGIN_VIA,
                         "control setup: the forged stamp really is in the v1 snapshot")
        # A governed bump adds the personal material the way only a
        # compromised writer could — still no binding row.
        self.store.bump_input_version(
            task_id,
            payload_overrides={
                "approved_facts": [evidence["selector"]],
                "public_context": {"evidence_summaries": {
                    document_a["document_id"]: {
                        "summary": "R15样本公告甲（fixture，非真实披露）",
                        "sha256": document_a["sha256"]}}},
            },
            actor=OWNER, reason="control: unbound task claims personal material")
        self._install_replay_answer_no_citation(task_id)
        result = self._run_replay_worker(task_id, self.config)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        stored = self.store.get_task(task_id)
        model_call = stored["model_call"]
        delivery = model_call["context_delivery"]
        self.assertEqual(delivery["delivered_evidence"], [],
                         "the shared view does not know the personal document")
        self.assertIn("{0}:not_verified_in_vault".format(document_a["document_id"]),
                      delivery["dropped_evidence"])
        bundle = model_call["fact_bundle"]
        self.assertEqual(bundle["facts"], [])
        reasons = {item.get("reason") for item in bundle["excluded"]}
        self.assertIn("source_not_verified", reasons)
        # Counterpart sanity: the personal ENTRY task with the same actor
        # DOES resolve the owner's namespace (covered by the chain tests);
        # here the report honestly delivered nothing citable.
        self.assertNotIn("verified_facts", model_call["outbound_context"])

    def test_personal_entry_binds_owner_generic_and_dedup_do_not(self):
        """The binding is established ONLY by the personal entry, and a
        deduplicated retry keeps the original task's ownership."""
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        from stock_agent.personal_evidence import PERSONAL_ORIGIN_VIA
        from stock_agent.task_intake import ensure_question_task

        # 1) Personal entry (HTTP route, session actor): binding written in
        #    the create transaction.
        status, created = self._create_research(
            document_ids=[document_a["document_id"]])
        self.assertEqual(status, 200)
        personal_id = created["task_id"]
        binding = self.store.input_owner_binding(personal_id)
        self.assertEqual(binding["owner"], OWNER)
        self.assertEqual(binding["via"], PERSONAL_ORIGIN_VIA)
        self.assertTrue(binding["bound_at"])

        # 2) Generic intake with the same actor name: NO binding.
        generic = ensure_question_task(
            self.store, research_question="通用入口任务？", symbols=["600519"],
            config=self.config, actor=OWNER)
        self.assertIsNone(self.store.input_owner_binding(generic["task"]["task_id"]))

        # 3) Retry of the SAME personal submission dedups to the same task
        #    and keeps the original binding untouched.
        status, retry = self._create_research(
            document_ids=[document_a["document_id"]])
        self.assertEqual(status, 200)
        self.assertEqual(retry["outcome"], "DUPLICATE")
        self.assertEqual(retry["task_id"], personal_id)
        binding_again = self.store.input_owner_binding(personal_id)
        self.assertEqual(binding_again, binding)

    def test_tampered_selector_rejected_and_queue_untouched(self):
        evidence = self._prepare_owner_evidence()
        document_a = evidence["document_a"]
        tampered = dict(evidence["selector"], start=evidence["selector"]["start"] + 1)
        status, rejected = self._create_research(
            document_ids=[document_a["document_id"]], fact_selectors=[tampered])
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"], "invalid_fact_selector")
        # Fail-closed: nothing was created, so a corrected retry cannot be
        # swallowed as DUPLICATE.
        self.assertIsNone(self.store.claim_next("w-r15-facts"))

    def test_foreign_namespace_selector_rejected(self):
        """A selector minted in ANOTHER owner's namespace never rides this
        user's task (the entry verifies against the OWNER's own view)."""
        foreign = self._import(
            FOREIGN_OWNER, ANNOUNCEMENT_A, "foreign.html",
            "他人命名空间样本公告（fixture）")
        foreign_view = personal_vault_view(self.config, FOREIGN_OWNER)
        text, _ = _document_text(foreign_view, foreign)
        start = text.index(FACT_SENTENCE_HEAD)
        excerpt = create_excerpt(foreign_view, foreign["document_id"],
                                 start, text.index("。", start) + 1)
        status, rejected = self._create_research(
            document_ids=[foreign["document_id"]],  # also unknown for OWNER
            fact_selectors=[excerpt_selector(excerpt)])
        self.assertEqual(status, 400)
        self.assertIn(rejected["error"],
                      ("invalid_fact_selector", "unknown_document"))
        self.assertIsNone(self.store.claim_next("w-r15-facts"))


if __name__ == "__main__":
    unittest.main()
