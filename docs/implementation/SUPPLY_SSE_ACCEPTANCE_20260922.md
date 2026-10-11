# Supply binding and resilient task board acceptance

Date: 2026-09-22. Implementer: Codex. Continuation of
`ISOLATED_ACCEPTANCE_PROGRESS_20260922.md`.

## Delivered behavior

### Signal store -> task -> retained document -> worker

1. Supply reads the configured `storage.sqlite_path` using a read-only SQLite URI.
   It does not initialize/migrate a database or silently choose the default database
   when a configured database is absent. Missing/unavailable stores are explicit
   selection states, and yield no evidence bindings.
2. Eligible news/filing records must match the requested symbols, have a valid
   publication time inside the window (120-second future tolerance), be public
   under the existing classification policy, and not carry example/replay/test
   provenance. Undeclared-symbol and malformed records cannot authorize a binding.
3. `bound_signal_ids` comes from these stored records, never from the vault alone.
   Document selection intersects these bindings with the authoritative vault
   document's `signal_ids` BEFORE applying the five-document delivery limit.
4. The worker rereads stored bindings at execution time. Deleted records, changed
   classification, changed scope, missing/malformed bindings and unlinked documents
   cannot deliver evidence. Invalid lookbacks fail closed, including overflow-sized
   integers. `context_delivery.signal_binding` records status/counts/verified IDs.
5. Shared egress governance receives symbols from the declared scope, quote keys
   and independently resolved stored bindings. Removing task `symbols` no longer
   hides a bound restricted instrument from the active restriction list.
6. Genuine temporary SignalStore + vault importer + real CLI + task store + worker
   tests cover the producer/consumer path. Worker execution is replay and model
   calls remain zero. Governance enabled/enforce stays true in these new tests.

Bounds: at most 2,000 stored records examined and 200 bindings selected per scope;
selection reports truncation. Supply defaults to a 48-hour window. Supported
lookbacks are 1..8,784 hours. Worker rechecks the declared window, so queue delay
can make evidence ineligible; it is not silently refreshed or substituted.

This is signal/symbol scope binding, NOT institutional case authorization. The
document summary still derives from vault title metadata. It is NOT a verified
extraction of financial statements or an approved evidence-grounded prose summary.
Do not infer a real research conclusion or release permission from this delivery.

### Task board resilience

- Reconnecting EventSource actively refreshes the snapshot, including when no new
  event is available for replay.
- Non-2xx and malformed task snapshots preserve the last valid table.
- Existing response sequence guard prevents a delayed older response from
  overwriting newer data; an older failed response cannot replace current feedback.
- Browser verification uses real Chromium, HTTP POST, EventSource, a local TCP
  interruption proxy and temporary databases. No synthetic dispatch substitutes
  for network reconnect. The proxy observes actual Last-Event-ID request headers.
- A second tab verifies SSE delivery independently of the submitting tab's fetch.

## Acceptance evidence

- `test_task_signal_binding.py`: 14 tests PASS with the write/network/child guard.
  Includes configured-path producer/worker flow, real CLI creation, missing/corrupt
  store, irrelevant documents before the cap, public/fresh/scope/provenance checks,
  deletion, reclassification, forged scope, null/malformed binding, read-only file
  hash stability, unlinked documents, invalid lookback, and restricted-list checks.
- `test_isolated_test_guard.py`: 4 PASS. Added percent-encoded local SQLite URI
  handling without permitting a workspace database or a remote URI host.
- Existing targeted tests: supply 4 PASS, D-round review 18 PASS, B-round review
  13 PASS, first product slice 18 PASS, board SSE 3 PASS.
- Real Chromium driver: five scenario groups PASS:
  1. Replay CLI, focus/node/value/selection retention, real POST, dirty clearing,
     second-tab SSE update.
  2. TCP disconnect, RECONNECTING, native cursor resume, missed task delivery,
     no duplicate events.
  3. Delayed older HTTP response cannot replace a newer rendered snapshot.
  4. HTTP 503 snapshot leaves the last successful table intact.
  5. Malformed HTTP 200 snapshot leaves the table intact; reconnect recovers it
     without any new events to trigger a refresh.
- Final full guarded regression: see the final result appended below. Subprocess
  launch denials remain a separate unclosed verification boundary, not passes.

## Reproduce

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_task_signal_binding.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_task_board_sse.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
# Existing reviewed Playwright module and existing Chromium, no installation:
$env:PLAYWRIGHT_MODULE='<absolute path to existing playwright module>'
$env:PLAYWRIGHT_CHROMIUM_EXECUTABLE='<absolute path to installed chrome.exe>'
node tests/browser_board_isolated.cjs
```

The Node driver runs Python application code only under `.venv312` with bytecode
disabled and the write guard installed. Its server and database are temporary and
terminated/removed on exit. The Python Playwright variant was updated consistently
but was not run because that library is absent from the locked environment.

## Files

- Production: `task_supply.py`, `evidence_context.py`, `research_worker.py`,
  `task_board.py`.
- Test infrastructure: `scripts/run_isolated_tests.py`, `tests/_vault_fixture.py`,
  `test_task_supply.py`, `test_task_signal_binding.py`, `test_isolated_test_guard.py`,
  `test_task_board_sse.py`, `browser_board_isolated.cjs`, `browser_board_e2e.py`.

## Final guarded regression

Locked Python `-B`, final code: **965 tests in 106.427 seconds; errors=14,
skipped=2; exit code 1**. Every error entry is `PermissionError: test isolation:
unguarded child process denied` in the known PowerShell parser, process lifecycle
and junction suites. No other test error or assertion failure was reported.
Do not present this as 965 passes or treat subtest errors as 14 test methods.
Those suites require their separate isolation boundary before full acceptance.

After validation, the real vault ledger is still 0 bytes with LastWriteTime
2026-09-21 16:20:55. The temporary browser server was terminated and its temporary
database removed. No existing business server was stopped or restarted.

## Still not production GO

No credential, real ledger, provider approval, roster or scheduled task was edited.
No live LLM was invoked. These are temporary synthetic replay cases, not business
samples. The vault deletion incident still needs independent human review; no
historical event was invented, removed or restored in this batch.

Next gates remain:
1. PowerShell/lifecycle/junction acceptance in a disposable OS-level environment
   without real business data or secrets, rather than bypassing the Python guard.
2. Evidence excerpts/summary versions bound to exact retained source spans and
   review provenance; title metadata alone must not become a financial claim.
3. An approved narrow W10 real-event slice, with valid public-source permission,
   real retained original, verified human identity and explicit live-model evidence.
   Preserve NO_GO until its real business prerequisites are satisfied.
