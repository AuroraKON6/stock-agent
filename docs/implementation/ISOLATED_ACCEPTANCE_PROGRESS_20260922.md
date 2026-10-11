# Test isolation and real browser acceptance - 2026-09-22

## Scope and status

Implemented by Codex on top of the existing Zcode changes. No business ledger,
production configuration, credential, provider approval or task schedule was edited.
This is technical progress, not production GO. The prior vault deletion incident
still requires independent human review; no missing historical evidence was recreated.

## Changes

- `scripts/run_isolated_tests.py`: installs a Python audit guard before discovery.
  Denies checkout writes (including SQLite), external connections, and unguarded
  child processes. Loopback and temporary-directory writes remain available.
  This prevents accidental Python writes, not malicious native-code activity.
- `tests/test_isolated_test_guard.py`: three tests for write/database/move denial,
  temporary writes, loopback, external connections and child process denial.
- `tests/test_acceptance_fixes_20260921.py`: concurrent report writers now each
  enter their own temporary path context; background exceptions fail the test.
  The old parent-only context did not propagate to threads and attempted to write
  production reports. The guard blocked the attempts during this run.
- `tests/test_market_resilience.py`: temporary context for the fallback tests;
  the verification-provider test previously attempted to replace the real cache.
- Page dump defaults to temporary files; BOARD_TEST_OUTPUT_DIR explicitly retains
  fixtures outside the checkout for the standalone browser checker.
- `tests/browser_board_e2e.py`: removed production local configuration; example
  config and database now live in a temporary workspace. Every project subprocess
  uses locked Python, the write guard, and a temporary project root. Startup is
  awaited and child termination is reaped. Python Playwright driver not executed
  here because the locked environment does not contain that package.
- `tests/browser_board_isolated.cjs`: executable browser alternative using the
  existing Node Playwright package and installed Chromium. Python project imports
  and CLI execution remain under the locked interpreter with the guard enabled.
- AGENTS.md documents the guarded entry point and its limitations.

## Verification performed

- Guard tests: 3/3 PASS.
- Existing context/vault review tests: 18/18 PASS under guard.
- Acceptance tests including threaded output: 14/14 PASS under guard after fix.
- Market resilience: 12/12 PASS under guard after fix.
- Page dump: 1/1 PASS under guard after fix.
- Real Chromium: PASS for isolated CLI replay -> awaiting_human -> real HTTP POST
  -> succeeded. Reordering preserves the exact focused node, text and selection.
  Successful submission leaves no dirty form. A second tab that never submits or
  manually refreshes receives the succeeded state through SSE and shows LIVE.
  This is synthetic replay UI evidence, not real research or human authorization.
- Full guarded discovery: 950 tests, 116.675 seconds, **errors=14, skipped=2**.
  All remaining error entries are denied subprocess launches in PowerShell parse,
  lifecycle and Windows junction tests. The report/cache/page write failures from
  the first guarded run no longer occur. This is NOT an all-green full regression.
  Error count includes subtests and must not be treated as number of test methods.
- Real vault events file remains 0 bytes with modification time
  2026-09-21 16:20:55. This verifies no new mutation of that file in this run, not
  that the earlier deletion was lossless.

## Reproduce

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_isolated_test_guard.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_review_fixes_20260921c.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
# Set PLAYWRIGHT_MODULE to an existing reviewed Playwright installation.
# Set PLAYWRIGHT_CHROMIUM_EXECUTABLE if that package's default browser is absent.
node tests/browser_board_isolated.cjs
```

## Next gates

1. Run PowerShell/lifecycle/junction tests in a disposable OS-level environment
   without real business data or credentials. Do not silently allow child processes
   or skip failed cases to manufacture a green full-suite result.
2. Review supply-generated task association: build_task_spec does not currently
   declare bound_signal_ids while the worker requires them for associated documents.
   Derive association from authoritative case/signal data, not arbitrary document
   claims; add producer-to-worker coverage before calling that path complete.
3. Add browser disconnect/reconnect and stale-response ordering scenarios. Current
   successful two-tab SSE delivery does not prove interruption recovery.
4. Independently review the recorded vault incident. Keep surviving evidence.
   No production activation, scheduled sampling or W10 live claim in this batch.
