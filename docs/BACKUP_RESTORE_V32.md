# Backup / Restore Drill v1

## 1. Purpose

This control proves that selected A-share research fact sources can be captured,
verified, and restored into an isolated directory without changing live research
state. It is a local technical rehearsal, not a production disaster-recovery
approval.

The SQLite copy uses Python's `sqlite3.Connection.backup()` API so the database
snapshot is consistent even when the source connection is in use. See the
[Python sqlite3 backup documentation](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
and the [SQLite Online Backup API](https://www.sqlite.org/backup.html).

## 2. Commands And Order

```powershell
python -m stock_agent backup-create --config config/watchlist.local.json --require-pass
python -m stock_agent backup-verify --config config/watchlist.local.json --backup latest --require-pass
python -m stock_agent restore-drill --config config/watchlist.local.json --backup latest --require-pass
python -m stock_agent backup-restore-status --config config/watchlist.local.json --require-ready
```

`--require-ready` checks only the local technical rehearsal. Production disaster
recovery is a separate gate:

```powershell
python -m stock_agent backup-restore-status --config config/watchlist.local.json --require-production-ready
```

The production command must continue to fail until externally verified controls
and independent human sign-off exist.

## 3. Included Recovery Facts

- The configured primary SQLite database, copied with the SQLite backup API.
- Non-test `data/**/*.jsonl` fact and audit ledgers, excluding compact projections
  and UAT/test directories.
- Review action JSONL history under `reports/actions/`.
- Content-addressed official-evidence objects and the current vault index.
- Dependency locks, example policy configuration, pinned Pi runtime metadata, and
  reviewed Pi method bundles.
- A generated `config/watchlist.restore.json` with secret-bearing fields redacted
  and URL credentials/query strings removed.

Every file is listed by relative path, byte count, and SHA-256. JSONL files are
parsed line by line; ledgers carrying `event_hash` and `previous_hash` are also
verified as chains. SQLite files require `PRAGMA quick_check=ok`, stable schema
hashes, and matching table row counts. Evidence-object paths must match their
content hash.

## 4. Deliberately Excluded State

- Raw `*.local.json`, `.env*`, model/Pi credentials, and authentication stores.
- Human roster and restriction-list originals.
- Value-validation sealed A/B mappings.
- Rebuildable `latest`, dashboard, compact-event, cache, and ordinary report
  projections.
- Application source code; source recovery requires a reviewed Git baseline or a
  separately controlled source artifact.

These exclusions prevent a clear-text same-device rehearsal from becoming a new
credential, personal-data, restriction-list, or blinding leak. Production backup
must cover the excluded operational state through an approved encrypted archive.

## 5. Restore Drill

The drill accepts only a bundle below the configured `bundle_root`. It rejects
absolute/traversal manifest entries, symbolic links, missing or extra files,
manifest tampering, file tampering, invalid JSONL, broken hash chains, corrupt
SQLite files, and secret-like payloads.

Restore output is always created below `data/backup_restore/drills/`. It never
overwrites the live database, config, ledger, evidence vault, or business state.
`recovery_projection.json` is rebuilt from restored facts and must match the
manifest's fact projection hash.

## 6. RPO And RTO Interpretation

- The configured RPO target is initially 24 hours. Backup age can be checked, but
  one manual snapshot does not prove a recurring operational RPO.
- The configured RTO target is initially 30 minutes. The measured value covers
  only restoration and validation of the selected data/configuration bundle.
- Full-service RTO also requires source checkout, dependencies, credential
  recovery, service startup, downstream reconciliation, user acceptance, and
  independent sign-off. v1 does not claim those steps.
- Non-SQLite files are individually stable-copied and rehashed. They are not an
  atomic multi-file snapshot until the scheduler has an application-wide lease
  and checkpoint protocol.

## 7. Production Gate

Production readiness remains `BLOCKED` until all of the following are evidenced:

1. A clean, reviewed Git HEAD and recoverable remote/source artifact.
2. An approved encryption/KMS design and encrypted recovery of sensitive local
   operational state.
3. An approved offsite or immutable/WORM target with retention and deletion
   controls.
4. An application-wide lease/checkpoint proving cross-file snapshot consistency.
5. A scheduled-backup history proving RPO rather than a one-time local copy.
6. A timed full-service restore on separate infrastructure.
7. Independent human review by IT Operations/InfoSec/Records owners, recorded in
   the approved external control-evidence system.

Software cannot grant these approvals. It must report missing evidence as
`BLOCKED`, without creating people, contracts, provider authorization, archive
evidence, schedules, or business activation.

## 8. Safety Boundary

No command in this control accesses the network, exports credentials, calls an
LLM, installs a scheduled task, changes a research case, approves or publishes
research, or performs a trade/order/position action.
