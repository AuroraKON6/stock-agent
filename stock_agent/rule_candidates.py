"""R22: rule changes produce PENDING REVIEW CANDIDATES only.

A rule (role flow / prompt) change never reaches the approved version by
being written: :func:`propose_rule_change` derives a NEW frozen artifact
with its own digest, records it as ``pending_review``, and leaves the
registry and every approved digest exactly as they were. Only
:meth:`RuleCandidateBook.approve` — with a named HUMAN actor outside the
automated/model-role namespaces — registers and approves the candidate.

This is the rule-side counterpart of the digest-bound approvals in
``role_execution``: there, an old approval never covers a new digest; here,
a new digest never even becomes runnable until a human approves it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import now_iso
from .role_execution import (
    FlowRegistry,
    RoleFlowError,
    define_role_flow,
    prompt_artifact,
)

RULE_CANDIDATE_SCHEMA = "1.0"


class RuleCandidateError(ValueError):
    """reason codes: malformed_change, unknown_role, candidate_not_found,
    not_pending, reviewer_not_human"""


def _human_reviewer(reviewer: Any) -> str:
    from .personal_review import actor_kind  # local import: no import cycle

    name = str(reviewer or "").strip()
    if actor_kind(name) != "human":
        raise RuleCandidateError(
            "reviewer_not_human: {0!r} is an automated/model identity, "
            "not an approver".format(name))
    return name


def _candidate_flow(base_flow: Dict[str, Any],
                    changes: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise RuleCandidateError("malformed_change: changes must be a non-empty object")
    unexpected = set(changes) - {"prompts", "groups", "note"}
    if unexpected:
        raise RuleCandidateError(
            "malformed_change: unknown change fields {0}".format(sorted(unexpected)))
    base_prompts = base_flow.get("prompts") or {}
    prompt_changes = changes.get("prompts") or {}
    if not isinstance(prompt_changes, dict):
        raise RuleCandidateError("malformed_change: prompts must be an object")
    prompts: Dict[str, Dict[str, Any]] = {}
    for role, artifact in base_prompts.items():
        override = prompt_changes.get(role) or {}
        if not isinstance(override, dict) or set(override) - {"version", "text"}:
            raise RuleCandidateError(
                "malformed_change: prompt override for {0} must carry version/text".format(role))
        prompts[role] = prompt_artifact(
            role,
            override.get("version", artifact.get("version", 1)),
            override.get("text", artifact.get("text", "")),
        )
    unknown_roles = sorted(set(prompt_changes) - set(base_prompts))
    if unknown_roles:
        raise RuleCandidateError(
            "unknown_role: {0} is not part of flow {1}".format(
                unknown_roles, base_flow.get("flow_id")))
    groups = changes.get("groups") or base_flow.get("groups")
    return define_role_flow(
        flow_id=str(base_flow.get("flow_id") or ""),
        version=int(base_flow.get("version") or 0) + 1,
        groups=groups,
        prompts=prompts,
    )


def propose_rule_change(base_flow: Dict[str, Any], *, changes: Dict[str, Any],
                        actor: str) -> Dict[str, Any]:
    """Derive the candidate artifact — pending review, never applied.

    The base flow object is not mutated and no registry is touched: the
    returned record is the whole change until a human approves it."""
    candidate_flow = _candidate_flow(base_flow, changes)
    base_digest = str(base_flow.get("flow_sha256") or "")
    note = str((changes or {}).get("note") or "")[:500]
    return {
        "schema_version": RULE_CANDIDATE_SCHEMA,
        "candidate_id": "rule_candidate::{0}".format(
            str(candidate_flow["flow_sha256"])[:16]),
        "status": "pending_review",
        "created_by": str(actor or ""),
        "created_at": now_iso(),
        "note": note,
        "flow_id": str(candidate_flow["flow_id"]),
        "base": {"version": int(base_flow.get("version") or 0),
                 "flow_sha256": base_digest},
        "candidate": {
            "version": int(candidate_flow["version"]),
            "flow_sha256": str(candidate_flow["flow_sha256"]),
            "groups": candidate_flow["groups"],
            "prompt_sha256": {role: artifact["sha256"]
                              for role, artifact in candidate_flow["prompts"].items()},
            "prompts": candidate_flow["prompts"],
        },
        "applies_to_approved": False,
        "approved_version_unchanged": base_digest,
        "requires_human_approval": True,
        "flow": candidate_flow,
    }


class RuleCandidateBook:
    """In-memory candidate ledger (the offline slice's host-owned store).

    ``propose`` only records; ``approve`` is the single path that mutates a
    :class:`FlowRegistry`, and it refuses any reviewer that is not a named
    human."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def propose(self, base_flow: Dict[str, Any], *, changes: Dict[str, Any],
                actor: str) -> Dict[str, Any]:
        record = propose_rule_change(base_flow, changes=changes, actor=actor)
        stored = dict(record)
        stored.pop("flow", None)  # the live artifact stays internal to the book
        self._items[str(record["candidate_id"])] = {"record": stored, "flow": record["flow"]}
        return dict(stored)

    def get(self, candidate_id: str) -> Dict[str, Any]:
        entry = self._items.get(str(candidate_id))
        if entry is None:
            raise RuleCandidateError("candidate_not_found: {0}".format(candidate_id))
        return dict(entry["record"])

    def candidate_flow(self, candidate_id: str) -> Dict[str, Any]:
        """The unapproved artifact (for registry checks; never auto-approved)."""
        entry = self._items.get(str(candidate_id))
        if entry is None:
            raise RuleCandidateError("candidate_not_found: {0}".format(candidate_id))
        return dict(entry["flow"])

    def pending(self) -> List[Dict[str, Any]]:
        return [dict(entry["record"]) for entry in self._items.values()
                if entry["record"]["status"] == "pending_review"]

    def approve(self, candidate_id: str, *, registry: FlowRegistry,
                reviewer: str) -> Dict[str, Any]:
        """Register + approve the candidate — human reviewer only."""
        entry = self._items.get(str(candidate_id))
        if entry is None:
            raise RuleCandidateError("candidate_not_found: {0}".format(candidate_id))
        record = entry["record"]
        if record["status"] != "pending_review":
            raise RuleCandidateError(
                "not_pending: {0} is {1}".format(candidate_id, record["status"]))
        name = _human_reviewer(reviewer)
        flow = entry["flow"]
        registry.register(flow)
        registry.approve(flow, actor=name)
        record.update({"status": "approved", "approved_by": name,
                       "approved_at": now_iso(), "applies_to_approved": True,
                       "approved_version_unchanged": record["base"]["flow_sha256"]})
        return dict(record)
