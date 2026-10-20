"""R04: connector runtime — transactional query intake, host-side retention
rights, and execution policy for outbound data connectors.

Split out of the Kimi adapter so every future connector (direct HTTP, MCP)
shares the same three disciplines:

1. **Atomic query intake** (:mod:`.queries`): one SQLite database owns
   idempotency, per-source rate windows, per-upstream quota groups and the
   reserved -> dispatched -> terminal attempt chain. Two workers racing the
   same query key get ONE dispatch (A18); the last quota unit can never be
   double-spent (A19). The append-only JSONL ledger stays as audit output.

2. **Host-side retention rights** (:mod:`.rights`): a single
   ``RetentionDecision`` covers raw response artifacts, normalized cache,
   logs and fact-bundle content, derived from the registry entry — never from
   caller arguments (A20).

3. **Execution policy** (:mod:`.policy`): registered-purpose gate, exact
   destination resolution from host config, and rate/quota verdicts that
   refuse BEFORE any bytes are sent (A24/A25).
"""
from .policy import (
    DestinationRefused,
    assert_purpose_registered,
    assert_transport_destination,
    cache_rights_revision,
    cache_ttl_seconds,
    quota_group_for,
    resolve_destination,
)
from .queries import ConnectorQueryStore, status_for_outcome
from .rights import RetentionDecision, decide_retention

__all__ = [
    "ConnectorQueryStore",
    "DestinationRefused",
    "RetentionDecision",
    "assert_purpose_registered",
    "assert_transport_destination",
    "cache_rights_revision",
    "cache_ttl_seconds",
    "decide_retention",
    "quota_group_for",
    "resolve_destination",
    "status_for_outcome",
]
