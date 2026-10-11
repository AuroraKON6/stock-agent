# Run Coordination & Checkpoint v1

## Purpose

Run Coordination v1 gives the local research runtime one durable execution
identity per mode. It answers three operational questions without making a
research or trading decision:

1. Which process currently owns `run` or `agent-run`?
2. Which metadata-only stages have completed for that run?
3. Is a previous lease stale, corrupted, or still active?

The coordination ledger is an operational fact source. It is not a source of
market facts, an approval record, a publication control, or a production DR
control.

## Files

- `data/run_coordination/leases/<mode>.json`: one active lease per mode.
- `data/run_coordination/recovered/`: explicitly archived expired leases.
- `data/run_coordination/checkpoints.jsonl`: append-only SHA-256 hash chain.
- `reports/run_coordination/latest.json`: machine-readable status projection.
- `reports/run_coordination/latest.md`: operator-readable status projection.
- `logs/scheduled/run_*.summary.json`: scheduler summary including the
  coordination preflight and final status.

Lease files contain only a token digest, run id, timestamps, process id and
host. The raw token is held in process memory only. Checkpoints contain stage
names, counts, fingerprints and output file hashes; signal text, prompts,
credentials and model responses are not written to this ledger.

## Runtime contract

When `run_coordination.enabled` is true, `run` and `agent-run`:

1. atomically create a per-mode lease;
2. append `run_started`;
3. append a checkpoint after each major pipeline stage;
4. hash generated report/review/action files as output references;
5. append `run_completed` or `run_failed`;
6. release only the lease owned by that process.

The same mode cannot be run concurrently. Different modes may coexist. A
stale lease is a blocking condition and is never silently stolen.

## Operator commands

Read-only status, with no data collection or model call:

```powershell
python -m stock_agent run-coordination-status --config config/watchlist.local.json
python -m stock_agent run-coordination-status --config config/watchlist.local.json --mode agent-run --require-ready
```

Explicitly recover one confirmed expired lease:

```powershell
python -m stock_agent run-recover-stale `
  --config config/watchlist.local.json `
  --mode agent-run `
  --reason "已确认上一个进程已退出且租约已过期"
```

The command archives the lease and appends a reason to the hash chain. It
rejects active leases, empty reasons, malformed ledgers and paths outside the
workspace. It never retries the research run automatically.

## Failure and recovery semantics

- Lease conflict: fail before collection; do not call the LLM or downstream
  handoff stages.
- Stage exception: append `run_failed`, release the owned lease, and preserve
  the original exception for the caller.
- Ledger corruption: fail closed; repair requires human-controlled inspection
  and must not overwrite history.
- Missing or stale output files: output references are incomplete and the
  status remains operationally reviewable; no content is copied into the
  coordination ledger.
- Isolated test runs: coordination is bypassed so tests cannot alter the
  production run ledger.

## Production boundary

The current implementation is local technical coordination only. It does not
prove cross-host fencing, scheduler identity, business continuity, encrypted
off-site recovery, human approval, or production enablement. Therefore the
status projection intentionally reports `production_readiness: BLOCKED` until
those independent controls are evidenced and approved.

## Next extension

The next safe extension is a calendar-aware pilot reconciler that consumes an
exact completed daily run id and manifest. It must reuse this lease/checkpoint
fact source, remain idempotent, and add no automatic business or trading
action.
