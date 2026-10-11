# Local Summary Review Acceptance

Date: 2026-09-23, Asia/Shanghai. Implementer: Codex.
Continuation of `EXCERPT_NATIVE_ACCEPTANCE_20260922.md` and the existing W06/W08/W09
work before the W10 real-event slice. This is an implementation/technical test
delivery, not real-person UAT, a business sample or production GO.

## Delivered

1. **Local account binding**: `stock_agent/local_identity.py` queries the Windows
   process token through native read-only APIs, rejects impersonated threads and
   session zero, and compares the result with the claimed actor's unique recorded
   SID. Roster enforcement must be literal true; active human, public-side
   information-analyst role, coverage and restriction checks remain mandatory.
   Review requires explicit `summary_review_authorized`. No user is enrolled and
   no binding or permission is approved by this code or this delivery.
2. **Version-bound review**: `stock_agent/summary_review.py` records local drafts
   and independent review decisions in the EXISTING task SQLite database. Draft,
   task version, outbox and command receipt commit atomically. Authors/reviewers
   need different actor IDs and Windows subjects. Original excerpt selectors are
   revalidated against the real vault and this task's current stored signals.
3. **Clear provenance**: model text must match the persisted task summary; edits
   become human judgment. The draft records exact summary/source versions, task
   input/context hash, role/prompt versions, replay/live mode and model-called flag.
   A review binds draft_id, summary_id, context and task version, actor and rationale.
4. **Lifecycle**: pending -> supported / rejected / changes_requested; supported
   can be explicitly revoked. New draft revisions supersede old ones. A 24-hour
   expiry, changed task context, unavailable/tampered sources or current identity
   policy mismatch invalidate effective support on read. Historical rows remain.
5. **No approval escalation**: all review results remain local-only,
   external_eligible=false and institutional_approval=false. Nothing is rerun,
   published, enabled, dispatched, traded or marked institutionally approved.
   Ordinary personal task acknowledgement cannot satisfy summary review.
6. **CLI**: `task-summary --action show|submit|review`. Show uses readonly SQLite,
   including old schemas without migrations. Mutations authenticate before opening
   the writable store, then recheck inside the transaction. This CLI path does not
   load model credentials or create the global project directory tree.
7. **Board**: readonly summary status in task rows; existing outbox/SSE refresh
   updates it. Same-version status refresh does not destroy a dirty feedback form.
   Mobile horizontal overflow is contained in the dense table's scroll region.
   No summary-write HTTP route or summary-approval form was added.
8. **Shared receipt correction**: generic human responses now also bind receipt
   IDs to exact operation/task/actor/content/version. A summary command ID cannot
   be reused to falsely acknowledge another action. Old unhashed receipts remain
   intact but return conflicts rather than claim verified exact replay.

Operating details, commands and assurance limitations:
`docs/SUMMARY_REVIEW_LOCAL_V1.md`.

## Actual Verification

| Scope | Result |
| --- | --- |
| New `test_summary_review.py` | 23 tests OK; temporary vault + SignalStore + task DB + synthetic identity bindings |
| Existing `test_first_product_slice.py` | 18 tests OK after receipt change |
| Real Chromium driver, final code | 6 scenario groups PASS; desktop 1440x900 and mobile 390x844 screenshots inspected |
| `task-summary --help` | Exit 0, registered CLI surface |
| Full guarded locked-Python suite | 1013 tests, 79.999 seconds, errors=14, skipped=2, exit 1 |
| Updated native source bundle | PREPARED and VERIFIED; native_execution=NOT_RUN |

The full suite is NOT GREEN. It retains the native process isolation boundary:
PowerShell AST parser tests, workbench lifecycle/reused-PID tests and junction
tests receive `PermissionError: test isolation: unguarded child process denied`.
There are 14 error entries including subtests, not 14 independent test methods.
No assertion failures were reported. Do not subtract these counts to invent a
pass total or remove the guard to produce a green run.

The new unit/integration tests cover wrong account, nonhuman/inactive/malformed
principal, missing role/permission, symbol restrictions, same-person review,
source/task association, exact version, receipt reuse, rollback, two-connection
stale writes, immutable history, expiry/revocation, source tampering, readonly old
schema and migration compatibility, real CLI dispatch and HTTP boundaries.
They include one real READ-ONLY Windows token probe. No real account SID was
printed, stored or enrolled by that test. Synthetic review decisions prove code
paths, not that real independent people have reviewed anything.

Chromium groups:

1. Readonly summary badge rendering/invalidation while the input node remains dirty;
   screenshots and no page-level mobile horizontal overflow. Badge states in this
   rendering check are injected synthetic snapshots, not fabricated backend reviews.
2. Real replay CLI -> task board HTTP -> personal feedback POST -> second-tab SSE,
   input/focus/selection retention and dirty clearing.
3. Real TCP disconnect, RECONNECTING, Last-Event-ID observed on the wire and missed
   task event replay without duplicate events.
4. Delayed older snapshot does not replace newer state.
5. HTTP 503 leaves the last good table intact.
6. Malformed HTTP 200 preserves the table; reconnect refresh recovers even without
   new events.

Summary-specific decisions/outbox/HTTP projections are covered by integration
tests; the browser driver does not impersonate real reviewers or prove a real
summary-review identity session. The temporary server was terminated on exit.
No running business workbench was restarted.

Screenshots:

- `output/playwright/summary-board-desktop.png`
- `output/playwright/summary-board-mobile.png`

## Reproduce

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_summary_review.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_first_product_slice.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
# Use already installed/reviewed Playwright and Chromium; do not download latest.
$env:PLAYWRIGHT_MODULE='<absolute existing playwright module path>'
$env:PLAYWRIGHT_CHROMIUM_EXECUTABLE='<absolute existing Chromium executable path>'
node tests/browser_board_isolated.cjs
```

Native package: `reports/native_acceptance_20260923.zip`, 97 source/synthetic files
plus manifest, 563,858 bytes. The prior package was NOT overwritten.

```text
SHA-256: 185c989e27b1683047fd658e4dd4a2a448667bc19817446349055ccdca90e889
```

```powershell
.\.venv312\Scripts\python.exe -B scripts/prepare_native_acceptance.py --verify reports/native_acceptance_20260923.zip --sha256 185c989e27b1683047fd658e4dd4a2a448667bc19817446349055ccdca90e889
```

The ZIP targets the original three native acceptance suites. The new summary
tests were run under the host guard and are not added to that native test bundle.
Follow `docs/NATIVE_ACCEPTANCE_RUNBOOK.md` only in a disposable Windows VM.

## Files

- Added: `stock_agent/local_identity.py`, `stock_agent/summary_review.py`,
  `tests/test_summary_review.py`, `docs/SUMMARY_REVIEW_LOCAL_V1.md`, this report.
- Updated: `stock_agent/task_runtime/store.py`, `stock_agent/cli.py`,
  `stock_agent/task_board.py`, `tests/browser_board_isolated.cjs`,
  `docs/implementation/BASELINE.md`.
- Generated: the two screenshots and the updated immutable source ZIP.

No real configuration, credentials, roster, restrictions, supplier permission,
business event or scheduled task was changed. No live model or collection call.
The real vault ledger remains 0 bytes, LastWriteTime 2026-09-21 16:20:55. The prior
incident and historical audit material were not cleaned or rewritten.

## Still Open / Next Order

1. Native PowerShell/lifecycle acceptance in an independently disposable OS remains
   unexecuted. The newly verified ZIP is preparation, not native test success.
2. Local OS account matching is NOT human-presence proof or browser authentication.
   Current browser actor fields remain self-reported for personal feedback only.
   Actual human bindings and independent CLI UAT have not occurred. Binding record
   references are not automatically accepted as control-evidence approvals.
3. Full WS20 protected signatures, DPAPI/HMAC or enterprise IdP and institutional
   gate integration are not delivered. Preserve its Git/value/real-person gates.
4. Next product step: authenticated detailed review access, followed by the narrow
   W10 real public-event slice when lawful-source, original, identity and live-model
   prerequisites really exist. Do not expand parallel roles before that acceptance.
5. Business NO_GO and independent review of the historical vault incident remain
   separate from these technical results.
