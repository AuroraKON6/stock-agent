# Local Version-Bound Summary Review

Implemented 2026-09-23. This is an internal research artifact review, not
institutional quality approval, compliance clearance, publication or trading.
`external_eligible` and `institutional_approval` remain false for every result.

## Workflow

1. A persisted task must contain a research summary and be `awaiting_human` or
   `succeeded`, with public input scope and explicit symbols.
2. A registered local operator submits a summary draft with one to five exact
   excerpt selectors from `evidence-excerpt`. Original bytes stay local.
3. The server rereads the authoritative vault and stored-signal bindings. Every
   excerpt must be valid and associated with this task, not merely in the vault.
4. `model_inference` text must equal the task's persisted summary. Edited text is
   `human_judgment`; neither label certifies that claims are supported by sources.
5. Another authorized local operator reviews the exact `draft_id`, `summary_id`
   and expected task version. The submitter and reviewer must have different
   actor IDs AND different Windows account SIDs.
6. Decisions: `supported`, `reject`, `request_changes`, `revoke`, all with a
   nonempty rationale. Support is a human judgment about this local draft only.
   A returned/rejected draft needs a new submitted revision. Only recorded support
   can be revoked; stale/expired support can still be explicitly revoked.
7. New drafts supersede older revisions without deleting their history. A draft
   expires 24 hours after submission. Reads recompute validity from task context,
   current identity/restriction policy and retained evidence. A changed task input
   or output, invalid source, or current policy mismatch makes support ineffective.

No review action completes, queues, reruns, dispatches, publishes or changes the
classification of its research task. `request_changes` is a request, not an
automatic model call. The existing `provide_input` task mechanism remains separate
and changes invalidate the summary review. A generic personal acknowledgement
does NOT make a summary `supported` and is not authenticated by this new feature.

## Local Account Assurance

The CLI queries the current Windows process token SID via documented native
interfaces. It rejects an impersonated thread, unavailable token, service session
zero, non-Windows runtime and a claimed actor whose configured SID differs.
It does not use USERNAME, a browser form field or an environment-supplied SID.
Handles and converted SID buffers are released without spawning `whoami`.

References: [OpenProcessToken](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken),
[GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation),
[ConvertSidToStringSidW](https://learn.microsoft.com/en-us/windows/win32/api/sddl/nf-sddl-convertsidtostringsidw),
[OpenThreadToken](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openthreadtoken).

This proves a local OS account, NOT physical human presence, browser identity,
independent legal identity or nonrepudiation. Processes running as the same account
share its authority. Agents must not invoke real review actions on behalf of a
person. The backend does not enroll actors, bind SIDs, approve binding evidence or
provision credentials. Administrators who can change the roster, code or database
are outside this local integrity threat model.

Required operator-owned configuration:

- `research_operations.enabled` and `enforce_roster` must be literal JSON `true`.
- Valid roster AND restriction files, with an active human, public-side
  `information_analyst`, correct symbol coverage and no active restriction.
- Exactly one actor per configured Windows subject. Each binding uses
  `identity.provider=windows_local`, `identity.subject`, timezone-aware `bound_at`
  and `bound_evidence_ref` referring to the operator's binding record.
- Review additionally requires `summary_review_authorized` in that person's
  `authorizations`. This permission is never added automatically.

The binding evidence reference is recorded provenance, not an automatically
validated control-evidence approval. Human bootstrap, independent review of those
records, protected signatures, DPAPI/HMAC or enterprise IdP are NOT delivered here.
The wider WS20 design remains incomplete; its source baseline/value/real-person
gates still apply. Do not use this local feature as a replacement for existing
institutional protected approval interfaces.

## CLI

The task database must already exist. `show` opens it read-only, does not migrate
it, load credentials or create project directories. Mutating commands authenticate
before opening the writable store and authenticate again inside the transaction.
Only invoke these commands as the actual authorized human operator:

```powershell
# Inspect the current task and any submitted local draft.
.\.venv312\Scripts\python.exe -B -m stock_agent task-summary --config config/watchlist.local.json --task-id <task-id> --action show

# The spec contains exactly text, content_kind, selectors. It is local-only.
.\.venv312\Scripts\python.exe -B -m stock_agent task-summary --config config/watchlist.local.json --task-id <task-id> --action submit --spec <local-spec.json> --actor <registered-submitter> --expected-version <current-task-version> --command-id <unique-command-id>

# An independent operator inspects the exact draft/excerpts with show first.
.\.venv312\Scripts\python.exe -B -m stock_agent task-summary --config config/watchlist.local.json --task-id <task-id> --action review --draft-id <draft-id> --summary-id <summary-id> --actor <registered-reviewer> --expected-version <current-task-version> --decision supported --notes <review-rationale> --command-id <unique-command-id>
```

Do not pass a fabricated actor or copy another user's SID. `show` returns local
excerpts and draft details on stdout for review, not for model input. With no real
bindings, writes correctly fail closed; the software must not create demo people
in the real roster to make this command pass.

## Persistence and Consistency

New tables live in the EXISTING task database: append-only `task_summary_drafts`
and `task_summary_reviews`. No second orchestrator or separate business ledger.
`BEGIN IMMEDIATE` commits the artifact, task row-version increment, outbox event
and idempotency receipt atomically. Schema addition preserves old tasks/events.
Readonly access to a pre-upgrade database returns `not_submitted` without DDL.

Each draft preserves the exact summary, source selectors, submitter binding,
input/context hash, source task version, role and prompt versions, execution mode
and reported model-call flag. Reviews bind the draft/summary/context version,
reviewer binding, rationale, task version and time. The history is append-only in
application APIs, not a cryptographically signed archive or an immutable OS file.

Idempotency receipts bind operation, task, actor, content and expected version.
Identical retries do not append again. Changed arguments or cross-operation reuse
fail with a conflict. Legacy human-response receipts without this request hash
are preserved but cannot be replayed as verified exact matches; inspect the old
task/events rather than automatically issuing a replacement approval.

`supported` is an effective projection, checked again when read. Temporary source
unavailability or policy mismatch makes it `invalidated`; restoring identical
valid inputs may restore the projection. An explicit `revoke` is retained and
cannot be undone by restoring files. This is NOT a background revocation watcher.

## Board Boundary

The task board displays a readonly summary status and refreshes it through existing
outbox/SSE snapshots. Expiry or policy changes become visible on the next refresh,
including the existing bounded SSE reconnect. The status badge updates even when
the task version is unchanged or its unrelated feedback form is dirty. On narrow
viewports the dense task table scrolls inside its container.

This feature adds no summary-write HTTP route and no summary approval form.
Existing form `actor` is self-reported and must not approve summaries. The summary
projection contains no excerpt body, SID or review rationale; CLI `show` remains a
local detailed review surface. A subsequent, separate `task-summary-open` command
now offers an account-issued, five-minute, read-only browser view of one exact
draft. See `docs/SUMMARY_VIEWER_LOCAL_V1.md` for its capability and expiry boundary.
It is not a browser approval API. Existing board access/authentication and personal
feedback limitations are not upgraded to a complete login system by this change.

## Remaining Gates

- Actual independent humans, approved identity-binding records and real CLI UAT.
- Enterprise browser authentication before protected institutional review controls;
  the new local view proves possession of account-issued viewing access only.
- Full WS20 signatures/control-evidence binding and institutional gate integration.
- Native PowerShell/lifecycle acceptance in its disposable Windows environment.
- W10 real public-event slice with lawful source use, retained original, genuine
  model-call evidence and real personal acknowledgement. Synthetic test decisions
  never count as people, approvals, shadow runs or value-validation samples.
