"""R06: research-output quality contract — claims, citations, perturbation.

Closes F13: previously an output was valid as long as the references it DID
give belonged to the approved set — nothing forced specific factual claims to
carry citations at all, so a fully un-cited "analysis" could complete a task.

The contract implemented here:

- **Specific vs inferred**: a finding (or the summary) carrying numbers is a
  SPECIFIC claim; text explicitly marked as inference (推断/假设/预计/… or the
  dedicated ``inferences`` list) is exempt but must stay marked.
- **A30**: a specific claim with NO citations blocks completion. The draft is
  preserved as an EVIDENCE GAP (A31: a gap statement without citations is
  legal and saveable — gaps describe what is missing, they assert nothing).
- **A32**: a cited number that does not appear in the delivered context text
  is FLAGGED for rule/human review (pointed out — the rule does not silently
  trust it, and does not auto-reject either).
- **A33**: the quality score is perturbation-sensitive — deleting citations
  flips the verdict and drops the score; mutating numbers/dates raises
  mismatch flags and drops the score. The report is stored with the output so
  reviewers see why a draft was downgraded or refused.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

QUALITY_SCHEMA_VERSION = "1.0"

# Text markers that explicitly mark inference/assumption (claimed, not fact).
INFERENCE_MARKERS = (
    "推断", "假设", "预计", "预期", "推测", "可能", "或许", "如果", "若",
    "inference", "hypothesis", "estimate", "expect", "likely", "may ",
)

# ASCII + full-width digits with optional decimal part.
_NUMBER_RE = re.compile(r"[0-9０-９]+(?:[.．][0-9０-９]+)?")

_SCORE_UNSUPPORTED_CLAIM = 40
_SCORE_NUMBER_MISMATCH = 15

VERDICT_OK = "ok"
VERDICT_COMPLETE_WITH_FLAGS = "complete_with_flags"
VERDICT_INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ResearchQualityError(ValueError):
    pass


def claim_texts(output: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a model output into claim records: {source, text, refs}.

    Findings may be plain strings or {text, evidence_refs} objects; the
    summary participates only as a claim source for citation checks.
    """
    claims: List[Dict[str, Any]] = []
    global_refs = [str(ref) for ref in (output.get("evidence_refs") or [])]
    findings = output.get("key_findings") or []
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("finding") or "")
                refs = [str(ref) for ref in (item.get("evidence_refs") or [])]
            else:
                text, refs = str(item or ""), []
            if text.strip():
                claims.append({"source": "key_finding", "text": text,
                               "refs": refs or list(global_refs)})
    summary = str(output.get("summary") or "")
    if summary.strip():
        claims.append({"source": "summary", "text": summary,
                       "refs": list(global_refs)})
    return claims


def is_inference(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return any(marker.lower() in lowered for marker in INFERENCE_MARKERS)


def numeric_tokens(text: str) -> Set[str]:
    return {match.group(0) for match in _NUMBER_RE.finditer(str(text or ""))}


def _normalize_token(token: str) -> str:
    return token.replace("０", "0").replace("１", "1").replace("２", "2") \
        .replace("３", "3").replace("４", "4").replace("５", "5") \
        .replace("６", "6").replace("７", "7").replace("８", "8") \
        .replace("９", "9").replace("．", ".")


def context_text_for_quality(safe_context: Dict[str, Any]) -> str:
    """Deterministic text of EVERYTHING delivered to the model: evidence
    summaries (vault projection + explicitly-unverified human notes) and the
    approved quote snapshots. A number is verifiable only if it was delivered."""
    parts: List[str] = []
    evidence = safe_context.get("evidence_summaries") if isinstance(safe_context, dict) else None
    if isinstance(evidence, dict):
        for ref in sorted(evidence):
            entry = evidence[ref] or {}
            parts.append(str(entry.get("summary") or ""))
            parts.append(str(entry.get("human_note") or ""))
    quotes = safe_context.get("quote_snapshots") if isinstance(safe_context, dict) else None
    if isinstance(quotes, dict):
        for symbol in sorted(quotes):
            entry = quotes[symbol] or {}
            parts.append("{0} price={1} pct_change={2}".format(
                symbol, entry.get("price"), entry.get("pct_change")))
    return "\n".join(parts)


def assess_draft(output: Dict[str, Any], *, context_text: str = "",
                 explicit_inferences: Optional[List[str]] = None) -> Dict[str, Any]:
    """Score one model draft. Never mutates the output; returns the report."""
    explicit = {str(item) for item in (explicit_inferences or [])}
    explicit.update(str(item) for item in (output.get("inferences") or []))
    specific: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []
    inference_like: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    for claim in claim_texts(output):
        text = claim["text"]
        if text in explicit or is_inference(text):
            inference_like.append(claim)
            continue
        tokens = numeric_tokens(text)
        if not tokens:
            continue
        entry = dict(claim, numbers=sorted(tokens))
        specific.append(entry)
        if not claim["refs"]:
            unsupported.append(entry)
            continue
        if context_text:
            normalized_context = _normalize_token(context_text)
            for token in sorted(tokens):
                if _normalize_token(token) not in normalized_context:
                    mismatches.append(dict(entry, unmatched_number=token))
    score = 100
    score -= _SCORE_UNSUPPORTED_CLAIM * len(unsupported)
    score -= _SCORE_NUMBER_MISMATCH * len(mismatches)
    score = max(0, score)
    if unsupported:
        verdict = VERDICT_INSUFFICIENT_EVIDENCE
    elif mismatches:
        verdict = VERDICT_COMPLETE_WITH_FLAGS
    else:
        verdict = VERDICT_OK
    return {
        "schema_version": QUALITY_SCHEMA_VERSION,
        "verdict": verdict,
        "score": score,
        "specific_claims": specific,
        "unsupported_specific_claims": unsupported,
        "inference_marked": inference_like,
        "number_mismatch": mismatches,
        "evidence_refs_count": len([str(r) for r in (output.get("evidence_refs") or [])]),
        "review_note": (
            "具体结论缺少引用：草稿按缺证据保存，不能作为完成的研究交付（A30/A31）"
            if unsupported else
            "引用存在但部分数字未在已投递上下文中出现：规则已指出，需人工复核（A32）"
            if mismatches else ""),
    }
