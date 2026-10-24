"""One governed Pi analysis node: research task in, schema-valid draft out.

First slice of plan W08, hardened per acceptance review 2026-09-21. The
worker claims nothing itself: it runs one already-leased task. EVERY attempt
re-runs the real egress governance (classification, secrets, personal data,
non-public terms, prompt injection, restrictions) over the CURRENT payload —
including patches that arrived through human ``provide_input`` — and the
outbound prompt is built from the governance-minimized safe payload. Denied
payloads are recorded as ``blocked`` with zero model calls. Model responses
are read through the ``response_metadata`` contract, awaited-human tasks
persist their full output, call audit, and every question before the handoff.
``execution_mode=replay`` skips the model call and is recorded as replay.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .agent_protocol import (
    IntentValidationError,
    ProtocolBroker,
    validate_model_output,
)
from .config import project_path
from .invocation_runtime import (
    ModelCallNotSent,
    ModelKnownFailure,
    ModelOutcomeUnknown,
    call_deadline_seconds,
    classify_exception,
    classify_to_exception,
    lease_heartbeat,
)
from .llm_agent import _get_llm_config, _is_auth_error, _parse_llm_json
from .models import AnalyzedSignal, now_iso
from .task_runtime import (
    STATUS_BLOCKED,
    STATUS_SUCCEEDED,
    TaskRuntimeError,
    TaskStore,
)

WORKER_SCHEMA_VERSION = "1.0"
RESEARCH_PROMPT_VERSION = "research_note_v3_rounds"
# Plan budget suggestion (section 4.2): W10 single case, at most 4 model
# attempts including schema-repair retries; every intermediate round is one
# attempt, so the cap bounds the whole run, not just the final answer.
MAX_RESEARCH_ROUNDS = 4
# Read-only data intents whose results may feed the next round. Proposal,
# human-request and draft actions never produce round input.
QUERY_INTENT_ACTIONS = {
    "read_public_quote_snapshot",
    "read_public_news_cluster",
    "read_verified_evidence_summary",
    "calculate_approved_indicator",
}

_SYSTEM_PROMPT = (
    "You are a disciplined A-share research analyst. You receive one public research "
    "context and must answer with a single JSON object only, no prose outside the JSON. "
    "Required JSON fields: schema_version (\"1.0\"), task_id, summary (<=4000 chars), "
    "key_findings (list of strings, each tied to evidence), unknowns (list of strings "
    "for what the context does not establish), evidence_refs (list of context evidence "
    "ids you relied on; cite ONLY ids present in the context), human_questions (list of "
    "{question, why_needed, options}) for decisions or evidence only a human can "
    "provide, model_self_assessed_confidence (0-1, self-assessed, not a probability of "
    "being right), and optionally requested_intents (list of intents for approved "
    "read-only actions). The task may run several rounds: if you need more data from "
    "the approved read-only actions, return requested_intents with a provisional "
    "summary and the next round will deliver the executed results; return your final "
    "note (no query intents) once the context is sufficient. Round budget is limited, "
    "so request only what you need. Do not invent prices, do not treat fund-flow "
    "statistics as confirmed trades, and mark anything unverified as an unknown."
)

_EXTERNAL_FORBIDDEN = (
    ("local path", ":\\"), ("local path", "\\"),
    ("local path", "/home/"), ("local path", "/Users/"),
    ("secret", "sk-abcdefgh12345678-placeholder"),
    ("secret env name", "DEEPSEEK_API_KEY"),
    ("restricted roster", "restriction_list"),
)


class ResearchWorkerError(RuntimeError):
    pass


class _BudgetExhausted(ResearchWorkerError):
    """The atomic budget reservation was denied; carries the decision dict."""

    def __init__(self, decision: Dict[str, Any]):
        super().__init__(str(decision.get("reason") or "budget exhausted"))
        self.decision = decision


def assert_payload_external_safe(text: str) -> None:
    """Belt-and-braces minimization gate behind the real governance screen."""

    lowered = str(text or "").lower()
    for label, needle in _EXTERNAL_FORBIDDEN:
        if needle.lower() in lowered:
            raise ResearchWorkerError("outbound payload failed the external minimization gate: {0}".format(label))


def _egress_carrier(task: Dict[str, Any], config: Dict[str, Any]) -> AnalyzedSignal:
    from .task_supply import load_research_signals

    payload = dict(task.get("input_payload") or {})
    declared = payload.get("symbols")
    symbols = {s for s in declared if isinstance(s, str) and s} if isinstance(declared, list) else set()
    context = payload.get("public_context") or {}
    quotes = context.get("quote_snapshots") if isinstance(context, dict) else None
    if isinstance(quotes, dict):
        symbols.update(str(s) for s in quotes)
    # Resolve stored bindings independently of task-declared scope so an input
    # patch cannot hide a restricted instrument by removing/changing symbols.
    bound, _ = load_research_signals(config, signal_ids=payload.get("bound_signal_ids") or [])
    for signal in bound:
        symbols.update(signal["symbols"])
    return AnalyzedSignal(
        signal_id=str(task["task_id"]),
        source="task_runtime",
        source_type="research_task",
        title=str(task.get("title") or "research task"),
        summary=str(payload.get("research_question") or ""),
        url="",
        published_at=str(task.get("created_at") or now_iso()),
        collected_at=now_iso(),
        symbols=sorted(symbols),
        payload=payload,
    )


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _retained_vault_hashes(config: Dict[str, Any]) -> set:
    """SHA-256 set the vault ITSELF verified (chain, object bytes, size)."""

    from .evidence_context import verified_evidence_documents

    return set(verified_evidence_documents(config).keys())


def _quote_governance_verdicts(quotes: Dict[str, Any], config: Dict[str, Any], now=None) -> Dict[str, Dict[str, Any]]:
    """Run the REAL market-data-governance gate over candidate snapshots.

    Each snapshot becomes a price signal carrier and goes through
    ``assess_market_signals`` — the same verdict the signal pipeline uses.
    Freshness, future times, missing/unknown timestamps, provider
    authorization, and cross-source consensus are decided there, not by any
    weaker local rule. Returns {symbol: gate}.
    """

    from .market_data_governance import assess_market_signals
    from .models import RawSignal

    carriers = []
    for symbol, snapshot in quotes.items():
        if not isinstance(snapshot, dict):
            continue
        provider = str(snapshot.get("provider") or "unknown")
        observed_at = str(snapshot.get("observed_at") or now_iso())
        payload = {
            "code": str(symbol),
            "latest": snapshot.get("price"),
            "pct_change": snapshot.get("pct_change"),
            "quote_timestamp": snapshot.get("source_time"),
            "observed_at": observed_at,
            "market_resilience": {"provider": provider, "fetched_at": observed_at},
        }
        carriers.append(RawSignal(
            source=provider,
            source_type="price",
            title="{0} {1} quote".format(provider, symbol),
            summary="",
            url="",
            published_at=observed_at,
            payload=payload,
        ))
    assessed, _summary = assess_market_signals(carriers, config or {}, now=now)
    verdicts: Dict[str, Dict[str, Any]] = {}
    for signal in assessed:
        gate = (signal.payload or {}).get("market_data_governance") or {}
        verdicts[str((signal.payload or {}).get("code") or "")] = gate
    return verdicts


def build_safe_research_context(payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None, *, now=None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the research-task-specific minimized context for external egress.

    Facts are verified by the SAME authorities the pipelines use: quotes must
    pass the market-data-governance assessment (llm_eligible), evidence must
    be verified-and-retained by the official vault (fail-closed on any
    missing/tampered object). The delivery record states exactly what reached
    the model so an empty context can never masquerade as an evidence-based
    analysis.
    """

    context = payload.get("public_context") if isinstance(payload.get("public_context"), dict) else {}
    quotes = context.get("quote_snapshots") if isinstance(context.get("quote_snapshots"), dict) else {}
    evidence = context.get("evidence_summaries") if isinstance(context.get("evidence_summaries"), dict) else {}
    verdicts = _quote_governance_verdicts(quotes, config or {}, now=now)
    from .evidence_context import verified_evidence_documents

    retained_documents = verified_evidence_documents(config)
    safe_quotes: Dict[str, Any] = {}
    dropped_quotes: list = []
    for symbol, snapshot in quotes.items():
        if not isinstance(snapshot, dict):
            continue
        gate = verdicts.get(str(symbol)) or {}
        if gate.get("llm_eligible") is not True:
            reasons = ";".join(str(item) for item in gate.get("reason_codes") or []) or str(gate.get("status") or "no_verdict")
            dropped_quotes.append("{0}:market_governance[{1}]".format(symbol, reasons))
            continue
        safe_quotes[str(symbol)] = {
            "price": snapshot.get("price"),
            "pct_change": snapshot.get("pct_change"),
            "provider": str(snapshot.get("provider") or "")[:64],
            "source_time": str(snapshot.get("source_time"))[:64],
            "governance_status": str(gate.get("status") or "PASS"),
        }
    safe_evidence: Dict[str, Any] = {}
    dropped_evidence: list = []
    local_excerpts: Dict[str, Any] = {}
    # Revalidate claimed bindings against the signal store at execution time.
    # Document hashes or a task's own public label cannot authorize a binding.
    # The delivered SUMMARY always
    # comes from the vault projection itself; any user-supplied summary text
    # is preserved separately as an explicitly-unverified human note.
    from .task_supply import load_research_signals

    symbols = payload.get("symbols")
    if symbols is not None and (not isinstance(symbols, list) or any(not isinstance(s, str) for s in symbols)):
        symbols = []
    lookback = payload.get("signal_lookback_hours")
    bound_signals, binding_status = load_research_signals(
        config, symbols=symbols, signal_ids=payload.get("bound_signal_ids") or [], lookback_hours=lookback)
    task_signal_ids = {item["signal_id"] for item in bound_signals}
    for ref, item in evidence.items():
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha256") or "")
        input_summary = str(item.get("summary") or "").strip()
        if not _SHA256_RE.fullmatch(sha) or not input_summary:
            dropped_evidence.append("{0}:invalid_contract".format(ref))
            continue
        document = retained_documents.get(sha)
        if document is None:
            dropped_evidence.append("{0}:not_verified_in_vault".format(ref))
            continue
        if str(ref) != str(document.get("document_id") or ""):
            dropped_evidence.append("{0}:ref_document_id_mismatch".format(ref))
            continue
        document_signals = {str(item) for item in (document.get("signal_ids") or [])}
        if not (document_signals & task_signal_ids):
            dropped_evidence.append("{0}:not_associated_with_task".format(ref))
            continue
        if "source_selection" in item:
            from .evidence_excerpts import EvidenceExcerptError, verify_excerpt_selection

            try:
                excerpt = verify_excerpt_selection(config or {}, item["source_selection"])
                if excerpt["document_id"] != ref:
                    raise EvidenceExcerptError("excerpt belongs to another document")
            except EvidenceExcerptError:
                dropped_evidence.append("{0}:invalid_source_selection".format(ref))
                continue
            # Retain the local review record only; raw source excerpts are not
            # added to the minimized model context or sent to external models.
            local_excerpts[str(ref)] = excerpt
        vault_summary = str(document.get("title") or "").strip() or "vault document"
        vault_summary = vault_summary[:1000]
        entry: Dict[str, Any] = {
            "summary": vault_summary,
            "summary_source": "vault_projection",
            "summary_scope": "title_metadata_only_not_body_analysis",
            "summary_sha256": hashlib.sha256(vault_summary.encode("utf-8")).hexdigest(),
            "sha256": sha,
            "document_id": str(document.get("document_id") or ""),
            "vault_verified": True,
        }
        if input_summary and input_summary != vault_summary:
            entry["human_note"] = input_summary[:1000]
            entry["human_note_status"] = "unverified_human_judgment_not_official_fact"
        safe_evidence[str(ref)] = entry
    safe_context = {
        "quote_snapshots": safe_quotes,
        "evidence_summaries": safe_evidence,
        "context_notes": {
            "delivered_quotes": sorted(safe_quotes.keys()),
            "dropped_quotes": sorted(dropped_quotes),
            "dropped_evidence": sorted(dropped_evidence),
        },
    }
    delivery = {
        "local_excerpt_records": local_excerpts,
        "signal_binding": dict(binding_status, verified_signal_ids=sorted(task_signal_ids)),
        "delivered_quotes": sorted(safe_quotes.keys()),
        "dropped_quotes": sorted(dropped_quotes),
        "delivered_evidence": sorted(safe_evidence.keys()),
        "dropped_evidence": sorted(dropped_evidence),
        "outbound_sha256": hashlib.sha256(
            json.dumps(safe_context, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    }
    return safe_context, delivery


def govern_egress(task: Dict[str, Any], config: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any], Dict[str, Any]]:
    """Run the shared LLM governance over the task's CURRENT payload.

    Returns ``(allowed, reason, governance_record, safe_payload)``. The safe
    payload is the minimized, approved projection the outbound prompt must be
    built from; it is empty when denied.
    """

    payload = dict(task.get("input_payload") or {})
    classification = str(payload.get("data_classification") or "").lower()
    if classification != "public":
        return False, "task input classification is not public", {
            "gate": "classification", "classification": classification, "decision": "denied",
        }, {}
    from .llm_governance import prepare_llm_batch

    carrier = _egress_carrier(task, config)
    approved, safe_payloads, stats = prepare_llm_batch([carrier], config, "research_task")
    if not approved:
        decision = carrier.llm_governance.get("research_task") or {}
        reasons = decision.get("reasons") or ["governance denied external egress"]
        return False, "; ".join(str(item) for item in reasons), {
            "gate": "llm_governance",
            "decision": decision.get("decision", "denied"),
            "reasons": reasons,
            "audit_id": decision.get("audit_id", ""),
        }, {}
    safe = safe_payloads.get(str(task["task_id"])) or payload
    return True, "", {"gate": "llm_governance", "decision": "approved"}, safe


def build_user_prompt(task: Dict[str, Any], safe_payload: Dict[str, Any], safe_context: Dict[str, Any],
                      prior_rounds: Optional[list] = None) -> str:
    prompt = {
        "schema_version": WORKER_SCHEMA_VERSION,
        "task_id": task["task_id"],
        "input_version": task["input_version"],
        "research_question": str(safe_payload.get("summary") or (task.get("input_payload") or {}).get("research_question") or ""),
        "public_context": safe_context,
    }
    if prior_rounds:
        prompt["prior_rounds"] = prior_rounds
    return json.dumps(prompt, ensure_ascii=False, default=str)


def run_research_task(
    store: TaskStore,
    task_id: str,
    *,
    worker_id: str,
    config: Optional[Dict[str, Any]] = None,
    ttl_seconds: int = 120,
    max_rounds: int = MAX_RESEARCH_ROUNDS,
) -> Dict[str, Any]:
    """Run one leased task to a terminal, blocked, or awaiting-human state.

    Live tasks step in bounded rounds: the model either returns its final
    note or requests data through whitelisted read-only intents; executed
    results are fed back and the next round re-runs the full governance and
    context verification. The round budget bounds total model attempts.
    Replay tasks stay single-shot (a recorded final answer).
    """

    config = config or {}
    max_rounds = max(1, int(max_rounds))
    runtime_cfg = config.get("task_runtime") or {}
    # Plan budget 4.2: a case's total wall-clock time is bounded (default 10
    # minutes); retries live inside the same budget and never extend it.
    try:
        case_time_limit = max(60, int(runtime_cfg.get("case_time_limit_seconds") or 600))
    except (TypeError, ValueError):
        case_time_limit = 600
    task = store.get_task(task_id)
    # R17/A76 (defense in depth): a task bound to a role flow belongs to the
    # role orchestrator alone — the Python full chain must not advance it.
    # The primary protection is the claim-path exclusion; this entry check
    # keeps a misrouted hand-off from spending even one attempt.
    if (task.get("input_payload") or {}).get("role_flow"):
        return {
            "status": "refused_role_flow",
            "model_call": {"mode": str(task.get("execution_mode") or "replay"),
                           "called": False, "rounds": [], "steps": []},
            "error": "role_flow_not_owned_by_full_chain",
        }
    store.mark_running(task_id, worker_id=worker_id, fencing_token=task["fencing_token"], expected_version=task["version"])
    task = store.get_task(task_id)
    started = time.perf_counter()

    model_call: Dict[str, Any] = {
        "mode": str(task.get("execution_mode") or "live"),
        "provider": "",
        "model": "",
        "request_model": "",
        "called": False,
        "usage": {},
        "duration_seconds": 0.0,
        "prompt_version": RESEARCH_PROMPT_VERSION,
        "rounds": [],
    }
    # R02/F04: a lease renewal failure during ANY call revokes this worker's
    # right to start NEW outbound requests, even if the between-rounds renewal
    # would happen to succeed afterwards.
    heartbeat_state: Dict[str, Any] = {"healthy": True, "beats": 0}

    def _renew_or_fail() -> Optional[Dict[str, Any]]:
        """Renew the lease; on failure record it and return the PERSISTED result.

        Returns None when the renewal succeeded. The returned status comes from
        the store (the failure may have requeued or cancelled the task, not
        necessarily terminated it) — never a guessed terminal label.
        """

        try:
            store.renew_lease(task_id, worker_id=worker_id, fencing_token=task["fencing_token"], ttl_seconds=ttl_seconds)
            return None
        except TaskRuntimeError as exc:
            model_call["error"] = "lease renewal failed: {0}".format(exc)
            fresh = store.fail(task_id, worker_id=worker_id, fencing_token=task["fencing_token"],
                               error=str(model_call["error"]), retryable=True, model_call=model_call)
            return {"status": str(fresh.get("status") or "failed"), "model_call": model_call,
                    "error": str(model_call["error"])}

    def _fail(error: str, *, retryable: bool) -> Dict[str, Any]:
        model_call["duration_seconds"] = round(time.perf_counter() - started, 3)
        fresh = store.fail(task_id, worker_id=worker_id, fencing_token=task["fencing_token"],
                           error=error, retryable=retryable, model_call=model_call)
        # R01/F05: report what the store actually persisted. A retryable
        # failure with attempts left REQUEUES (queued) and a cancel that
        # landed mid-run CANCELS; "failed" is only one of the outcomes.
        return {"status": str(fresh.get("status") or "failed"), "model_call": model_call, "error": error}

    def _blocked(reason: str, governance: Dict[str, Any]) -> Dict[str, Any]:
        model_call["governance"] = governance
        model_call["error"] = reason
        model_call["duration_seconds"] = round(time.perf_counter() - started, 3)
        fresh = store.mark_blocked(task_id, worker_id=worker_id, fencing_token=task["fencing_token"],
                                   error=reason, model_call=model_call)
        return {"status": str(fresh.get("status") or STATUS_BLOCKED), "model_call": model_call, "error": reason}

    def _invoke_model(user_prompt: str, round_index: int) -> Tuple[str, Dict[str, Any]]:
        """One model attempt; returns (raw_text, per_round_record_without_results).

        R02: every call walks the outbound attempt chain — the budget is
        reserved (existing atomic reservation), the attempt row is written
        (prepared) and marked dispatched BEFORE the request can leave, the
        lease is heartbeated on a separate connection during the call, and the
        terminal attempt record captures whether the request provably never
        left (sent=no), reached the provider (sent=yes), or may have been sent
        with no answer (outcome_unknown — never auto-retried).
        """
        from .pi_runtime import call_pi_agent
        from .model_operations import response_metadata

        llm_cfg = _get_llm_config(config)
        provider = {}
        for candidate in llm_cfg.get("providers", []):
            provider = candidate
            break
        provider_name = str(provider.get("name") or "deepseek_primary")
        request_model = str(provider.get("model") or llm_cfg.get("model") or "")
        if not model_call["provider"]:
            model_call["provider"] = provider_name
        if not model_call["request_model"]:
            model_call["request_model"] = request_model
        attempt = {
            "round": round_index,
            "provider": provider_name,
            "request_model": request_model,
            "prompt_sha256": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
            "started_at": now_iso(),
        }
        # Atomic budget reservation: today's attempt events are counted and
        # this attempt recorded in ONE transaction, so concurrent workers can
        # never both spend the last slot. The recorded event is the audit
        # trail even when the outcome stays unknown.
        from .budget_guard import reserve_model_attempt

        reservation = reserve_model_attempt(
            store, config, task_id, worker_id=worker_id,
            fencing_token=task["fencing_token"], attempt=attempt)
        if not reservation.get("reserved"):
            raise _BudgetExhausted(reservation)
        from .task_runtime.store import ATTEMPT_FAILED_KNOWN, ATTEMPT_OUTCOME_UNKNOWN, ATTEMPT_SUCCEEDED

        provider_timeout = int(provider.get("timeout") or llm_cfg.get("timeout") or 60)
        deadline = call_deadline_seconds(
            provider_timeout=provider_timeout, case_time_limit=case_time_limit,
            elapsed_seconds=time.perf_counter() - started)
        if deadline <= 0:
            # The case budget is exhausted before dispatch: provably not sent,
            # so this is a safe retryable failure, not an unknown outcome.
            raise ModelCallNotSent(
                RuntimeError("case deadline exhausted before model dispatch"),
                "sent=no stage=preflight")
        invocation = store.prepare_invocation_attempt(
            task_id, worker_id=worker_id, fencing_token=task["fencing_token"],
            round_index=round_index, provider=provider_name,
            request_model=request_model, prompt_sha256=attempt["prompt_sha256"],
            budget_event_id=str(reservation.get("event_id") or ""),
            deadline_seconds=deadline)
        attempt_id = str(invocation["attempt_id"])
        store.mark_invocation_dispatched(attempt_id, worker_id=worker_id,
                                         fencing_token=task["fencing_token"])
        round_started = time.perf_counter()
        runner = None
        try:
            with lease_heartbeat(store.db_path, task_id, worker_id=worker_id,
                                 fencing_token=task["fencing_token"],
                                 ttl_seconds=ttl_seconds) as heartbeat:
                runner = heartbeat
                response = call_pi_agent(
                    system_prompt=_SYSTEM_PROMPT,
                    user_content=user_prompt,
                    provider=provider,
                    config=config,
                    role_id=str(task.get("role_id") or "research_analyst"),
                    timeout=deadline,
                )
        except BaseException as exc:
            sent, stage = classify_exception(exc)
            error_text = "{0}: {1}".format(type(exc).__name__, str(exc)[:500])
            if runner is not None:
                heartbeat_state["healthy"] = runner.healthy
                heartbeat_state["beats"] = runner.beats
            try:
                store.complete_invocation_attempt(
                    attempt_id, worker_id=worker_id, fencing_token=task["fencing_token"],
                    state=(ATTEMPT_OUTCOME_UNKNOWN if sent == "unknown" else ATTEMPT_FAILED_KNOWN),
                    sent=sent, error=error_text,
                    heartbeat_beats=(runner.beats if runner else 0),
                    heartbeat_healthy=(runner.healthy if runner else True))
            except TaskRuntimeError:
                # The lease was lost during the call: the attempt row stays
                # dispatched and crash recovery will classify it; the typed
                # exception below still drives the task-level outcome (whose
                # own store write may then fail with fencing, as before).
                pass
            typed = classify_to_exception(exc)
            typed.attempt_id = attempt_id
            raise typed from exc
        heartbeat_state["healthy"] = runner.healthy if runner else True
        heartbeat_state["beats"] = (runner.beats if runner else 0)
        store.complete_invocation_attempt(
            attempt_id, worker_id=worker_id, fencing_token=task["fencing_token"],
            state=ATTEMPT_SUCCEEDED, sent="yes",
            heartbeat_beats=(runner.beats if runner else 0),
            heartbeat_healthy=(runner.healthy if runner else True))
        meta = response_metadata(response)
        record = {
            **attempt,
            "attempt_id": attempt_id,
            "actual_model": str(meta.get("model") or "unknown"),
            "request_id": str(meta.get("request_id") or ""),
            "usage": dict(meta.get("usage") or {}),
            "duration_seconds": round(time.perf_counter() - round_started, 3),
            "ended_at": now_iso(),
            "budget_reservation": {
                "calls_today": reservation.get("calls_today"),
                "limit": reservation.get("limit"),
            },
        }
        return str(response), record

    # --- Egress gate: re-run on EVERY attempt over the current payload. ---
    # Runs for replay too: a recorded answer still must not enter the task
    # from a denied payload, and the delivery record must exist for all modes.
    allowed, reason, governance, safe_payload = govern_egress(task, config)
    if not allowed:
        return _blocked(reason, governance)
    safe_context, delivery = build_safe_research_context(task.get("input_payload") or {}, config=config)
    model_call["context_delivery"] = delivery
    model_call["outbound_context"] = safe_context
    # R13: freeze the attempt's fact bundle and bind the EXACT outbound
    # payload hash (A63) — the model context is provably the approved
    # payload. Payload-declared facts with no host-policy basis and
    # registry-denied sources are excluded here with recorded reasons
    # (A60/A61); the bundle snapshot rides the model_call audit.
    try:
        from .fact_bundle import (FactBundleError, bind_outbound_payload,
                                  build_research_fact_bundle, payload_digest)

        payload_facts = (task.get("input_payload") or {}).get("approved_facts") or []
        fact_selectors = [item for item in payload_facts
                          if isinstance(item, dict) and "excerpt_sha256" in item]
        fact_records = [item for item in payload_facts
                        if isinstance(item, dict) and "excerpt_sha256" not in item]
        if fact_selectors or fact_records:
            bundle = build_research_fact_bundle(
                config, task, fact_selectors=fact_selectors,
                extra_facts=fact_records)
            # Only STILL-DELIVERABLE bundle facts enter the minimized model
            # context; exclusions live in the audit, never in the prompt.
            delivered_facts = [dict(fact, content=str(fact.get("content") or "")[:1000])
                               for fact in bundle.get("facts") or []]
            if delivered_facts:
                safe_context = dict(safe_context)
                safe_context["verified_facts"] = delivered_facts
            # A63: bind the payload hash AFTER the final context is assembled,
            # with the SAME canonical digest the delivery record shows.
            model_call["outbound_context"] = safe_context
            outbound_hash = payload_digest(safe_context)
            model_call["fact_bundle"] = bind_outbound_payload(bundle, safe_context)
            model_call["context_delivery"] = dict(
                delivery, fact_bundle_id=bundle["bundle_id"],
                outbound_sha256=outbound_hash)
    except FactBundleError as exc:
        model_call["fact_bundle_error"] = str(exc)[:300]

    if model_call["mode"] == "replay":
        # Single recorded final answer; no rounds, no model entry.
        try:
            payload = task.get("input_payload") or {}
            replay = payload.get("replay_response")
            if not isinstance(replay, dict):
                raise ResearchWorkerError("replay task has no recorded replay_response")
            raw_text = json.dumps(replay, ensure_ascii=False)
            model_call["replay_source"] = "task_input_payload"
        except Exception as exc:
            model_call["error"] = "{0}: {1}".format(type(exc).__name__, str(exc)[:500])
            return _fail(model_call["error"], retryable=True)
    else:
        # ---- bounded multi-round live loop ----
        prior_rounds: list = []
        final_output: Optional[Dict[str, Any]] = None
        for round_index in range(1, max_rounds + 1):
            current = store.cancel_if_requested(task_id, worker_id=worker_id, fencing_token=task["fencing_token"])
            if current["status"] == "cancelled":
                return {"status": "cancelled", "model_call": model_call}
            elapsed = time.perf_counter() - started
            if elapsed > case_time_limit:
                return _fail(
                    "case time limit exceeded after {0:.0f}s (limit {1}s); no further model rounds".format(
                        elapsed, case_time_limit),
                    retryable=False)
            if not heartbeat_state.get("healthy", True):
                # F04: a heartbeat renewal failed during a previous call. The
                # in-flight outcome was already recorded; this worker must not
                # dispatch anything new. The task requeues for a fresh lease.
                model_call["error"] = "lease heartbeat failed during a model call; no new outbound permitted"
                model_call["lease_heartbeat"] = dict(heartbeat_state)
                fresh = store.fail(task_id, worker_id=worker_id,
                                   fencing_token=task["fencing_token"],
                                   error=str(model_call["error"]), retryable=True,
                                   model_call=model_call)
                return {"status": str(fresh.get("status") or "failed"),
                        "model_call": model_call, "error": str(model_call["error"])}

            if round_index > 1:
                # Governance and context verification re-run on every round
                # over the current payload — never only once at task start.
                allowed, reason, governance, safe_payload = govern_egress(task, config)
                if not allowed:
                    return _blocked(reason, governance)
                safe_context, delivery = build_safe_research_context(task.get("input_payload") or {}, config=config)
                model_call["context_delivery"] = delivery
                model_call["outbound_context"] = safe_context

            user_prompt = build_user_prompt(task, safe_payload, safe_context, prior_rounds=prior_rounds)
            try:
                assert_payload_external_safe(user_prompt)
            except ResearchWorkerError as exc:
                return _blocked(str(exc), governance)
            renewal_failure = _renew_or_fail()
            if renewal_failure is not None:
                return renewal_failure
            try:
                raw_text, round_record = _invoke_model(user_prompt, round_index)
                model_call["called"] = True
                model_call["model"] = round_record["actual_model"]
                model_call["budget"] = dict(round_record.get("budget_reservation") or {})
                if round_record.get("request_id"):
                    model_call["request_id"] = round_record["request_id"]
                # Top-level usage aggregates every round; per-round detail stays
                # in the round records.
                for key, value in (round_record.get("usage") or {}).items():
                    current = model_call["usage"].get(key)
                    if isinstance(value, (int, float)) and isinstance(current, (int, float)):
                        model_call["usage"][key] = current + value
                    elif key not in model_call["usage"]:
                        model_call["usage"][key] = value
            except _BudgetExhausted as exc:
                model_call["budget"] = dict(exc.decision)
                return _blocked(exc.decision.get("reason") or "budget exhausted",
                                {"gate": "budget", "decision": "denied",
                                 "budget": dict(exc.decision)})
            except ModelCallNotSent as exc:
                # A06: the request provably never left this machine (pre-send
                # failure). A bounded retry is safe; the attempt chain already
                # records sent=no.
                model_call["error"] = "{0}: {1}".format(type(exc.original).__name__, str(exc.original)[:500])
                model_call["lease_heartbeat"] = dict(heartbeat_state)
                return _fail(model_call["error"], retryable=True)
            except ModelOutcomeUnknown as exc:
                # A07/A08: the request may have reached the provider with no
                # usable answer. Budget stays held, the attempt enters
                # reconciliation, and the task blocks — NO auto re-dispatch.
                model_call["error"] = "{0}: {1}".format(type(exc.original).__name__, str(exc.original)[:500])
                model_call["lease_heartbeat"] = dict(heartbeat_state)
                return _blocked(
                    "model invocation outcome unknown (attempt {0}); manual reconciliation required before any retry".format(
                        getattr(exc, "attempt_id", "")),
                    {"gate": "invocation_outcome_unknown", "decision": "denied",
                     "attempt_id": getattr(exc, "attempt_id", ""),
                     "sent": "unknown"})
            except ModelKnownFailure as exc:
                # The provider answered definitively (auth/quota/rejection or
                # an unusable response). The charge outcome is the provider's
                # statement; retrying is a deliberate decision above this
                # layer, so the task does not auto-requeue.
                model_call["error"] = "{0}: {1}".format(type(exc.original).__name__, str(exc.original)[:500])
                model_call["lease_heartbeat"] = dict(heartbeat_state)
                return _fail(model_call["error"], retryable=False)
            except Exception as exc:
                model_call["error"] = "{0}: {1}".format(type(exc).__name__, str(exc)[:500])
                return _fail(model_call["error"], retryable=not _is_auth_error(exc))

            try:
                parsed = _parse_llm_json(raw_text)
                output = parsed[0] if isinstance(parsed, list) and parsed else parsed
                allowed_refs = sorted(safe_context.get("evidence_summaries", {}).keys())
                output = validate_model_output(output, allowed_evidence_refs=allowed_refs)
                claimed_task_id = str(output.get("task_id") or task_id)
                if claimed_task_id != task_id:
                    raise IntentValidationError("model output task_id does not match this task")
                output["task_id"] = task_id
            except Exception as exc:
                return _fail("model output failed validation: {0}: {1}".format(
                    type(exc).__name__, str(exc)[:400]), retryable=True)

            query_intents = [intent for intent in (output.get("requested_intents") or [])
                             if str(intent.get("action") or "") in QUERY_INTENT_ACTIONS]
            if query_intents and round_index < max_rounds:
                broker = ProtocolBroker(public_context=safe_context)
                intent_results: list = []
                for intent in query_intents:
                    try:
                        intent_results.append(broker.execute_intent(intent))
                    except IntentValidationError as exc:
                        intent_results.append({
                            "status": "rejected", "action": str(intent.get("action") or ""),
                            "error": str(exc)[:300],
                        })
                round_record = {**round_record, "final": False,
                                "provisional_summary": str(output.get("summary") or "")[:500],
                                "intent_results": intent_results}
                model_call["rounds"].append(round_record)
                store.record_research_round(task_id, worker_id=worker_id,
                                            fencing_token=task["fencing_token"], record=round_record)
                prior_rounds = prior_rounds + [{
                    "round": round_index,
                    "provisional_summary": round_record["provisional_summary"],
                    "intent_results": intent_results,
                }]
                continue
            model_call["rounds"].append({**round_record, "final": True})
            if query_intents:
                # The last round still requested data it never received: the
                # provisional note must not masquerade as a final answer.
                return _fail(
                    "round budget exhausted after {0} rounds; unfulfilled data requests remain".format(max_rounds),
                    retryable=False)
            final_output = output
            break
        if final_output is None:
            return _fail(
                "round budget exhausted after {0} rounds; no final note produced".format(max_rounds),
                retryable=False)
        output = final_output
        raw_text = ""  # final note already parsed and validated per round

    if model_call["mode"] != "replay":
        model_call["duration_seconds"] = round(time.perf_counter() - started, 3)

    if model_call["mode"] == "replay":
        # Replay parsing/validation mirrors the live contract.
        model_call["duration_seconds"] = round(time.perf_counter() - started, 3)
        try:
            parsed = _parse_llm_json(raw_text)
            output = parsed[0] if isinstance(parsed, list) and parsed else parsed
            safe_context, _delivery = build_safe_research_context(task.get("input_payload") or {}, config=config)
            allowed_refs = sorted(safe_context.get("evidence_summaries", {}).keys())
            output = validate_model_output(output, allowed_evidence_refs=allowed_refs)
            claimed_task_id = str(output.get("task_id") or task_id)
            if claimed_task_id != task_id and claimed_task_id != "placeholder":
                raise IntentValidationError("model output task_id does not match this task")
            output["task_id"] = task_id
        except Exception as exc:
            return _fail("model output failed validation: {0}: {1}".format(
                type(exc).__name__, str(exc)[:400]), retryable=True)

    # ---- R06 quality gate: specific claims must be cited (A30/A31/A32) ----
    # Runs for BOTH modes on the validated output. A specific (numeric) claim
    # with no citations cannot complete the research: the draft is preserved
    # as an evidence gap on the blocked record. Uncited GAP statements are
    # fine — unknowns assert nothing. Cited numbers missing from the delivered
    # context are flagged for review (A32), not silently trusted.
    from .research_quality import assess_draft, context_text_for_quality

    quality = assess_draft(output, context_text=context_text_for_quality(safe_context or {}))
    output["quality"] = quality
    if quality["verdict"] == "insufficient_evidence":
        model_call["quality"] = quality
        model_call["evidence_gap_draft"] = output
        return _blocked(
            "specific findings carry numbers without any citation; the draft is "
            "saved as an evidence gap, not a completed analysis",
            {"gate": "evidence_quality", "decision": "denied",
             "unsupported_specific": [str(item.get("text") or "")[:200]
                                      for item in quality["unsupported_specific_claims"]],
             "quality_score": quality["score"]})

    broker_results: list = []
    for intent in output.get("requested_intents") or []:
        try:
            safe_context = model_call.get("outbound_context") or {}
            broker = ProtocolBroker(public_context=safe_context)
            broker_results.append(broker.execute_intent(intent))
        except IntentValidationError as exc:
            broker_results.append({"status": "rejected", "error": str(exc)[:300]})
    if broker_results:
        output["broker_results"] = broker_results

    questions = output.get("human_questions") or []
    if questions:
        first = questions[0]
        stored = store.request_awaiting_human(
            task_id,
            worker_id=worker_id,
            fencing_token=task["fencing_token"],
            human_request={
                "question": str(first.get("question") or ""),
                "why_needed": str(first.get("why_needed") or ""),
                "options": [str(option) for option in (first.get("options") or [])][:5],
                "questions": [
                    {
                        "question": str(item.get("question") or ""),
                        "why_needed": str(item.get("why_needed") or ""),
                        "options": [str(option) for option in (item.get("options") or [])][:5],
                    }
                    for item in questions
                ],
            },
            output=output,
            model_call=model_call,
        )
        # R01/F05: the persisted status decides — a cancellation that landed
        # before the handoff is 'cancelled', never 'awaiting_human'.
        return {"status": str(stored.get("status") or "awaiting_human"),
                "model_call": model_call, "output": output}

    stored = store.complete(task_id, worker_id=worker_id, fencing_token=task["fencing_token"], output=output, model_call=model_call)
    # R01/F05: complete() itself may persist 'cancelled' when the user cancelled
    # during the run; the worker must report that, not assume success.
    return {"status": str(stored.get("status") or STATUS_SUCCEEDED), "model_call": model_call, "output": output}
