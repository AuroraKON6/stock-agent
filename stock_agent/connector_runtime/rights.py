"""R04: host-side retention rights for connector payloads.

One ``RetentionDecision`` answers, for a registry entry at a moment in time,
which persistence lanes are allowed: raw response artifacts (provider bodies
on disk), the normalized cache, logs (metadata-only ledger records — always
allowed, they never carry content), and fact-bundle content destined for
external models. The decision derives ONLY from the registry entry; callers
cannot widen it with arguments (F10/A20).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict


@dataclass
class RetentionDecision:
    raw_artifacts: bool
    cache: bool
    logs: bool
    fact_bundle_content: bool
    reasons: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_artifacts": self.raw_artifacts,
            "cache": self.cache,
            "logs": self.logs,
            "fact_bundle_content": self.fact_bundle_content,
            "reasons": dict(self.reasons),
        }


def decide_retention(registry_status: Dict[str, Any]) -> RetentionDecision:
    """Fail-closed: content lanes require approved + unexpired + explicit flag.

    ``registry_status`` is the projection from sources.registry.registry_status
    (never the raw entry), so approval and expiry are evaluated by the same
    authority the gates use.
    """
    approved = str(registry_status.get("approval_state") or "") == "approved"
    unexpired = not bool(registry_status.get("expired"))
    body_allowed = bool(registry_status.get("body_retention_allowed"))
    external_send = bool(registry_status.get("external_send_allowed"))
    base_ok = approved and unexpired
    reasons: Dict[str, str] = {}
    if not approved:
        reasons["approval"] = "source approval state is not approved"
    if not unexpired:
        reasons["expiry"] = "source registration expired"
    if not body_allowed:
        reasons["body_retention"] = "body_retention_allowed is false for this entry"
    if not external_send:
        reasons["fact_bundle"] = "llm_external_send_allowed is false for this entry"
    return RetentionDecision(
        raw_artifacts=base_ok and body_allowed,
        cache=base_ok and body_allowed,
        # Metadata-only ledger rows never carry provider content.
        logs=True,
        fact_bundle_content=external_send,
        reasons=reasons,
    )
