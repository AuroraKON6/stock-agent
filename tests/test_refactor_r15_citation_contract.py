"""R15 round 3: the model request lists the originals a report may cite.

OFFLINE ONLY. Every "model" answer below is a local stub that reads the
request the worker really built; no provider is contacted and no provider
key exists in the test environment. The public sample is the real SSE
announcement on 灵康转债 (113610) delisting (SHA-256 41b121c4...25ed7),
reused as a read-only case — results here say nothing about a live model.

Chain under test, all through production entries: personal workspace import
→ excerpt + fact → workspace research → CLI ``task-run-next`` claim → the
single outbound boundary (``pi_runtime.call_pi_agent``) → citation
validation. Checks: the ids the request advertises equal the validator's
set; an answer citing them is accepted; wrong ids are still rejected;
undelivered documents never enter the set; empty set; a rebuilt later round.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.agent_protocol import IntentValidationError, validate_model_output
from stock_agent.config import project_path_override
from stock_agent.evidence_excerpts import _document_text, create_excerpt, excerpt_selector
from stock_agent.evidence_facts import build_fact
from stock_agent.identity_providers import PersonalSessionStore
from stock_agent.models import AnalyzedSignal, now_iso
from stock_agent.personal_evidence import import_personal_document, personal_vault_view
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

# TEST ACCOUNT for this offline class (the browser launcher uses its own,
# "a93-browser-user"); neither is a real user.
OWNER = "lead-analyst"
SIGNAL_ID = "sig-r15-citation-filing"
# Public disclosure, stored gzip-compressed so git never rewrites its bytes
# (and its original whitespace does not trip whitespace checks); CASE_BYTES is
# the byte-identical original, whose digest is asserted in setUp.
CASE_PATH = Path(__file__).resolve().parent / "fixtures" / "sse_113610_delisting_20261120.html.gz"
CASE_SHA256 = "41b121c4662329efcf162cc8bdad87dbeaf4dface4a1f20152447faf60c25ed7"
# Announcement address verified on the parent task KONM-4.
CASE_URL = "https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20261120_10838548.shtml"
FACT_SENTENCE_HEAD = "本所将于2026年12月1日"
# Explicitly NOT a real disclosure: the control document the task attaches
# but which is never associated, so it must never be delivered.
# SYNTHETIC control document written for this test — NOT an exchange page.
# The vault importer only admits reviewed official domains, so the URL below
# borrows the exchange host under a clearly non-existent /test-fixtures/
# path; its body and title both say it is a test placeholder.
CONTROL_URL = "https://www.sse.com.cn/test-fixtures/synthetic-r15-citation-control.html"
CONTROL_HTML = """<!DOCTYPE html><html><head><title>R15对照样本乙</title></head>
<body><h1>对照样本公告（测试占位，非真实披露）</h1>
<p>本文件是自动化测试写入的合成占位内容，不是任何机构或公司的披露。</p>
<p>对照样本净利润为 999.99 万元。</p></body></html>"""


def _final(task_id, refs, *, text="上证公告披露：" + FACT_SENTENCE_HEAD + "终止其在本所市场的上市交易和转股。"):
    return {
        "schema_version": "1.0", "task_id": task_id,
        "summary": "灵康转债（113610）于2026年12月1日终止上市交易和转股。",
        "key_findings": [{"text": text, "evidence_refs": list(refs)}],
        "unknowns": [], "evidence_refs": list(refs),
        "human_questions": [], "model_self_assessed_confidence": 0.6,
    }


class R15CitationContractTestCase(unittest.TestCase):
    def setUp(self):
        global CASE_BYTES
        CASE_BYTES = gzip.decompress(CASE_PATH.read_bytes())
        self.assertEqual(hashlib.sha256(CASE_BYTES).hexdigest(), CASE_SHA256)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Real path: the vault's reparse-point guard rejects the /var symlink.
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        import stock_agent.config as config_module

        original_root = config_module.PROJECT_ROOT
        config_module.PROJECT_ROOT = self.root
        self.addCleanup(setattr, config_module, "PROJECT_ROOT", original_root)
        self.config = vault_config(self.root, {})
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        # The test environment carries no provider credential.
        llm = self.config.get("llm") or {}
        for key in ("api_key", "api_key_env"):
            llm.pop(key, None)
        self.config["llm"] = llm

        from stock_agent.storage import SignalStore

        signals = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            signals.upsert_many([AnalyzedSignal(
                signal_id=SIGNAL_ID, source="manual", source_type="filing",
                title="上证公告（可转债摘牌）【2026】131号 灵康转债终止上市",
                summary="fixture signal for the public SSE sample", url=CASE_URL,
                published_at=now_iso(), collected_at=now_iso(), symbols=["113610"],
                payload={"data_classification": "public"},
            )])
        finally:
            signals.close()
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

        def authenticator(config, actor, symbols):
            if actor != OWNER:
                raise ValueError("unknown principal")
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-r15-citation", "policy_sha256": "d" * 64}

        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=PersonalSessionStore(ttl_seconds=1800),
            login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.cookie = self._login()

    # ---------------- helpers ---------------- #

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json",
                          "X-Task-Board-CSRF": str(self.server.csrf_token),
                          "Cookie": self.cookie})
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
                         json.dumps({"actor": OWNER, "symbols": ["113610"]}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            return (response.getheader("Set-Cookie") or "").split(";")[0]
        finally:
            conn.close()

    def _import(self, content: bytes, name: str, title: str, source_url: str):
        """Write the incoming file as raw bytes: text-mode writes would
        translate "\r\n" on Windows and change what the vault digests."""
        source = self.root / "incoming"
        source.mkdir(parents=True, exist_ok=True)
        path = source / name
        path.write_bytes(content)
        return import_personal_document(
            self.config, OWNER, str(path), source_url=source_url,
            title=title, published_at="2026-11-20T18:00:00+08:00")["document"]

    def _prepare_case(self, *, with_control=True):
        """The user's own actions through production APIs on the public case."""
        self.assertIn(b"\r\n", CASE_BYTES, "the original carries CRLF line endings")
        case = self._import(CASE_BYTES, "sse_113610.html",
                            "灵康转债终止上市公告（上交所，公开样本）", CASE_URL)
        view = personal_vault_view(self.config, OWNER)
        from stock_agent.evidence_vault import link_evidence_to_signal

        link_evidence_to_signal(
            view, document_id=case["document_id"], signal_id=SIGNAL_ID, actor=OWNER,
            actor_role="information_analyst", notes="public SSE sample linked to the task signal")
        text, extractor = _document_text(view, case)
        self.assertIn(extractor, ("body-extract-v2-html+utf8-codepoints-v1",
                                  "body-extract-v2-html+utf8-codepoints-v1"))
        start = text.index(FACT_SENTENCE_HEAD)
        end = text.index("。", start) + 1
        excerpt = create_excerpt(view, case["document_id"], start, end)
        control = None
        if with_control:
            control = self._import(CONTROL_HTML.encode("utf-8"), "control.html",
                                   "对照样本公告乙（合成占位，非真实披露）", CONTROL_URL)
        return {"case": case, "control": control, "view": view,
                "excerpt": excerpt, "selector": excerpt_selector(excerpt),
                "fact": build_fact(view, excerpt_selector(excerpt), fact_type="statement",
                                   statement=text[start:end], period="2026Q4")}

    def _create_research(self, evidence):
        body = {"question": "灵康转债何时终止上市交易和转股？", "symbols": ["113610"],
                "execution_mode": "live", "fact_selectors": [evidence["selector"]]}
        if evidence["control"] is not None:
            body["document_ids"] = [evidence["case"]["document_id"], evidence["control"]["document_id"]]
        status, created = self._request("POST", "/api/v1/workspace/create", body)
        self.assertEqual(status, 200, created)
        self.assertEqual(created["outcome"], "RECORDED", created)
        return created["task_id"]

    def _run_production_entry(self, answer):
        """The normal claim entry (CLI ``task-run-next`` with the APP config
        only), with the one outbound call replaced by a local stub. ``answer``
        receives the request the worker built and returns the model text."""
        from stock_agent.cli import _task_run_next
        from stock_agent.model_operations import LLMResponse

        requests = []

        def stub(*, system_prompt, user_content, **_kwargs):
            requests.append({"system_prompt": system_prompt, "prompt": json.loads(user_content)})
            text = answer(requests[-1]["prompt"], len(requests))
            return LLMResponse(json.dumps(text, ensure_ascii=False),
                               {"model": "local-stub", "request_id": "req-offline",
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        config = dict(self.config, task_runtime={"db_path": str(self.store.db_path)})
        config_file = self.root / "worker-config.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=stub) as spy:
            _task_run_next(str(config_file), "w-r15-citation", 120)
        self.requests = requests
        self.stub_calls = spy.call_count
        return self.requests

    def _assert_advertised_equals_enforced(self, prompt, stored):
        contract = prompt["citation_contract"]
        evidence = prompt["public_context"]["evidence_summaries"]
        self.assertEqual(contract["allowed_evidence_refs"], sorted(evidence))
        self.assertEqual(contract["allowed_evidence_refs"],
                         stored["model_call"]["context_delivery"]["delivered_evidence"])
        self.assertEqual(len(contract["allowed_evidence_refs"]), len(set(contract["allowed_evidence_refs"])))
        # The same set the validator enforces: every advertised id passes,
        # and nothing else in the context does.
        for ref in contract["allowed_evidence_refs"]:
            validate_model_output(_final("probe", [ref], text="s"),
                                   allowed_evidence_refs=contract["allowed_evidence_refs"])
        return contract

    def _assert_decoys_rejected(self, prompt, task_id, extra=()):
        contract = prompt["citation_contract"]
        allowed = contract["allowed_evidence_refs"]
        facts = prompt["public_context"].get("verified_facts") or []
        decoys = set(extra) | {item["sha256"] for item in prompt["public_context"]["evidence_summaries"].values()}
        decoys |= {item["fact_key"] for item in facts} | {"113610", "placeholder"}
        decoys -= set(allowed)
        self.assertTrue(decoys, "the request should carry non-citable look-alike values")
        for decoy in decoys:
            with self.assertRaises(IntentValidationError):
                validate_model_output(_final(task_id, allowed + [decoy]),
                                      allowed_evidence_refs=allowed)
        return decoys

    # ---------------- offline cases ---------------- #

    def test_request_lists_citable_originals_and_valid_id_is_accepted(self):
        evidence = self._prepare_case()
        case = evidence["case"]
        task_id = self._create_research(evidence)

        def answer(prompt, _index):
            # The stub knows nothing but the request: it copies the allowed ids.
            self.assertIn(case["document_id"], prompt["citation_contract"]["allowed_evidence_refs"])
            return _final(prompt["task_id"], prompt["citation_contract"]["allowed_evidence_refs"])

        requests = self._run_production_entry(answer)
        stored = self.store.get_task(task_id)
        self.assertEqual(stored["status"], "succeeded", stored.get("last_error"))
        self.assertEqual(self.stub_calls, 1)
        prompt = requests[0]["prompt"]
        self.assertEqual(prompt["task_id"], task_id)
        contract = self._assert_advertised_equals_enforced(prompt, stored)
        self.assertEqual(contract["allowed_evidence_refs"], [case["document_id"]])
        self.assertEqual(contract["fact_citations"],
                         [{"fact_key": evidence["excerpt"]["excerpt_id"],
                           "evidence_ref": case["document_id"]}])
        self.assertIn("citation_contract.allowed_evidence_refs", requests[0]["system_prompt"])
        # An associated excerpt makes its source citable; the unassociated
        # control document attached to the same task stays out of the request.
        self.assertIn(case["document_id"], stored["model_call"]["context_delivery"]["delivered_evidence"])
        self.assertIn("{0}:not_associated_with_task".format(evidence["control"]["document_id"]),
                      stored["model_call"]["context_delivery"]["dropped_evidence"])
        # (The delivery note names it only as a dropped "<id>:reason" entry — a
        # status label, never a delivered document.)
        self.assertNotIn(evidence["control"]["document_id"], prompt["public_context"]["evidence_summaries"])
        self.assertIn(FACT_SENTENCE_HEAD,
                      prompt["public_context"]["verified_facts"][0]["content"])
        # Bare digest, excerpt id, symbol, and the attached-but-undelivered
        # control document are all rejected by the validator's set.
        self._assert_decoys_rejected(prompt, task_id, [evidence["control"]["document_id"]])
        # Report citation traces to the original.
        self.assertEqual(stored["output"]["evidence_refs"], [case["document_id"]])
        self.assertEqual(stored["model_call"]["fact_bundle"]["evidence_documents"],
                         [{"sha256": CASE_SHA256, "document_id": case["document_id"]}])
        # The vault object is the untouched original file from the issue.
        self.assertEqual(case["sha256"], CASE_SHA256)
        self.assertEqual(stored["model_call"]["prompt_version"], "research_note_v3_rounds_citation_contract")
        self.assertEqual(stored["model_call"]["rounds"][0]["prompt_sha256"],
                         hashlib.sha256(json.dumps(prompt, ensure_ascii=False).encode("utf-8")).hexdigest())
        self.assertIn(stored["output"]["quality"]["verdict"], ("ok", "complete_with_flags"),
                      stored["output"]["quality"])
        self.assertFalse(stored["output"]["quality"]["unsupported_specific_claims"],
                         stored["output"]["quality"])
        self.assertIn(FACT_SENTENCE_HEAD + "终止其在本所市场的上市交易和转股。",
                      prompt["public_context"]["verified_facts"][0]["content"])

    def test_wrong_id_is_still_rejected(self):
        evidence = self._prepare_case()
        case = evidence["case"]
        task_id = self._create_research(evidence)

        def answer(prompt, _index):
            # The look-alike values a model reaches for when the citable id
            # is not spelled out: the digest, the excerpt/fact key, the symbol.
            return _final(prompt["task_id"],
                          [case["sha256"], evidence["excerpt"]["excerpt_id"], "113610"])

        self._run_production_entry(answer)
        stored = self.store.get_task(task_id)
        self.assertEqual(stored["status"], "queued", "a retryable validation failure requeues")
        self.assertIn("model output failed validation: IntentValidationError: output cites evidence "
                      "that is not part of the current approved context", stored["last_error"])
        self.assertFalse(stored.get("output"))
        self.assertNotIn("task_succeeded", [e["event_type"] for e in self.store.events(task_id)])

    def test_empty_round_set(self):
        evidence = self._prepare_case(with_control=False)
        # Research WITHOUT the document or the fact: nothing citable this round.
        body = {"question": "灵康转债何时终止上市交易和转股？", "symbols": ["113610"],
                "execution_mode": "live"}
        status, created = self._request("POST", "/api/v1/workspace/create", body)
        self.assertEqual(status, 200, created)
        task_id = created["task_id"]

        def answer(prompt, _index):
            self.assertEqual(prompt["citation_contract"]["allowed_evidence_refs"], [])
            return {"schema_version": "1.0", "task_id": prompt["task_id"],
                    "summary": "本轮上下文没有可引用的原件，无法确认日期。",
                    "key_findings": [], "unknowns": ["上下文未提供终止上市日期"],
                    "evidence_refs": [], "human_questions": [], "model_self_assessed_confidence": 0.2}

        requests = self._run_production_entry(answer)
        stored = self.store.get_task(task_id)
        self.assertEqual(stored["status"], "succeeded", stored.get("last_error"))
        self.assertEqual(self._assert_advertised_equals_enforced(requests[0]["prompt"], stored)
                         ["fact_citations"], [])
        self.assertEqual(stored["output"]["evidence_refs"], [])

    def test_citing_into_an_empty_set_is_rejected(self):
        evidence = self._prepare_case(with_control=False)
        status, created = self._request("POST", "/api/v1/workspace/create",
                                        {"question": "灵康转债何时终止上市？", "symbols": ["113610"],
                                         "execution_mode": "live"})
        task_id = created["task_id"]

        def answer(prompt, _index):
            # An id remembered from another case must not survive an empty round.
            return _final(prompt["task_id"], [evidence["case"]["document_id"]])

        self._run_production_entry(answer)
        stored = self.store.get_task(task_id)
        self.assertNotEqual(stored["status"], "succeeded")
        self.assertIn("not part of the current approved context", stored["last_error"])
        self.assertNotIn(evidence["case"]["document_id"], json.dumps(self.requests[0]["prompt"]))

    def test_later_round_rebuilds_the_contract(self):
        evidence = self._prepare_case()
        case = evidence["case"]
        task_id = self._create_research(evidence)

        def answer(prompt, index):
            refs = prompt["citation_contract"]["allowed_evidence_refs"]
            if index == 1:
                self.assertNotIn("prior_rounds", prompt)
                # An intermediate round asks for more data through the allowlist.
                return {"schema_version": "1.0", "task_id": prompt["task_id"],
                        "summary": "已见公告，核对原文摘要。", "key_findings": [],
                        "unknowns": [], "evidence_refs": refs, "human_questions": [],
                        "model_self_assessed_confidence": 0.4,
                        "requested_intents": [{
                            "action": "read_verified_evidence_summary",
                            "parameters": {"evidence_ref": refs[0]},
                            "reason_summary": "read the delivered summary before concluding"}]}
            self.assertEqual(prompt["prior_rounds"][0]["round"], 1)
            return _final(prompt["task_id"], refs)

        requests = self._run_production_entry(answer)
        stored = self.store.get_task(task_id)
        self.assertEqual(stored["status"], "succeeded", stored.get("last_error"))
        self.assertEqual(len(requests), 2)
        contracts = []
        for request in requests:
            prompt = request["prompt"]
            contracts.append(self._assert_advertised_equals_enforced(prompt, stored)["allowed_evidence_refs"])
            self._assert_decoys_rejected(prompt, task_id, [evidence["control"]["document_id"]])
        self.assertEqual(contracts, [[case["document_id"]], [case["document_id"]]])
        self.assertEqual(requests[1]["prompt"]["input_version"], requests[0]["prompt"]["input_version"])
        self.assertEqual([r["final"] for r in stored["model_call"]["rounds"]], [False, True])
        self.assertEqual(stored["output"]["evidence_refs"], [case["document_id"]])


if __name__ == "__main__":
    unittest.main()
