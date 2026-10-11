# Exact Excerpts and Native Acceptance Preparation

Date: 2026-09-22. Implementer: Codex.
Continues `SUPPLY_SSE_ACCEPTANCE_20260922.md`, within the existing W06/W08/W09
grounding and acceptance work before the W10 real-event slice. No new trading,
provider, autonomous approval or role-expansion scope was introduced.

## Delivered

### Exact local source excerpts

- Added `stock_agent/evidence_excerpts.py` and CLI `evidence-excerpt`.
- Retained UTF-8 text/plain excerpts carry document ID, original-byte SHA-256,
  decoded-text SHA-256, extractor version, code-point range and excerpt SHA-256.
  Reuse reconstructs from authoritative retained bytes and compares all fields.
- Revalidates whole-vault integrity, canonical paths, object size/hash and the
  actual bytes decoded after verification. Supports BOM removal only; preserves
  CRLF. Bad encoding, oversized input, invalid offsets, missing/tampered objects,
  wrong document ID and unsupported formats fail closed.
- Summary drafts bind one to five excerpt identities and the exact draft version.
  They remain unreviewed human judgments or model inferences, never source facts
  or semantic-support certificates. No author, reviewer or approval is fabricated.
- Worker accepts a `source_selection` only after existing task/signal/document
  checks and revalidation. Invalid explicit selections drop that evidence. Valid
  excerpt records stay in the LOCAL context-delivery receipt, not external input.
- External evidence is still title metadata only, now explicitly labeled as such.
  Its summary hash covers the actual delivered truncated title, not the longer
  original title. No claim of body-grounded live LLM research is made.

Operating contract: `docs/EVIDENCE_EXCERPTS_LOCAL_V1.md`.
Current limitations: UTF-8 text/plain only; no PDF/HTML extraction, UI excerpt
editor, authenticated summary-review ledger or external derived-summary release.

### Source-only native acceptance bundle

- Added `scripts/prepare_native_acceptance.py`: build/verify only; no subprocess,
  VM provisioning, downloads, extraction or test execution.
- Allowlisted Python source, the three native acceptance test files and their
  required PowerShell scripts; generated empty synthetic config. Does not read or
  copy real config, .env, profiles, databases, reports, logs, venv or Git metadata.
- Each selected file has size and SHA-256 in the manifest. Source secret-pattern
  scan errors contain only rule/path, never a matched credential or context. The
  scan is not a claim that all possible sensitive source content is detectable.
- Rejects source reparse points, missing files, oversized input, duplicate archive
  paths, path traversal, non-allowlisted contents, wrong member hashes, nonsynthetic
  config, missing external ZIP digest and output overwrite.
- Bundle states are PREPARED / VERIFIED with native_execution=NOT_RUN. Verification
  cannot be used to assert that native tests passed. Host guard stays unchanged.
- Runbook specifies disposable Windows VM, independently reviewed offline runtime,
  blocked outbound networking, no shared production drives/profiles, per-command
  outputs/exit codes, skips and exact-process cleanup evidence.

Artifact: `reports/native_acceptance_20260922.zip`.
95 source/synthetic files plus manifest, ZIP size 555,863 bytes.
Separately recorded whole-ZIP SHA-256:

```text
eb3f037a87c4405b044bc62560776b207bb1c57bd06214262f3cf939658b5b67
```

Build and verification both executed successfully. This is a technical transfer
artifact, not an encrypted business backup or OS sandbox. No available
`WindowsSandbox.exe` or `docker.exe` launcher was found during this batch's check;
no disposable OS environment was provisioned or native test launched.
Runbook: `docs/NATIVE_ACCEPTANCE_RUNBOOK.md`.

## Validation Actually Executed

| Command / suite | Result |
| --- | --- |
| Guarded `test_evidence_excerpts.py` | 14 tests OK |
| Guarded `test_native_acceptance_bundle.py` | 11 tests OK |
| Guarded `test_task_signal_binding.py` | 14 tests OK |
| Guarded `test_review_fixes_20260921c.py` | 18 tests OK |
| `evidence-excerpt --help` | Exit 0, new CLI registered |
| Source bundle build and SHA/member verification | PREPARED then VERIFIED, native NOT_RUN |
| Full `scripts/run_isolated_tests.py` | 990 tests, 123.719 seconds, errors=14, skipped=2, exit 1 |

The 14 error entries are ALL `PermissionError: test isolation: unguarded child
process denied`: three workbench PowerShell parse subtests, two lifecycle/PID
tests, seven Z002 parse subtests, two Z003 junction tests. There were no other
test errors and no assertion failures. Error entries include subtests and must
not be subtracted from total test methods to invent a pass count.
This full suite is NOT GREEN; native acceptance is still BLOCKED/NOT_RUN here.

Reproduce offline on the host:

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_evidence_excerpts.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_native_acceptance_bundle.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
.\.venv312\Scripts\python.exe -B scripts/prepare_native_acceptance.py --verify reports/native_acceptance_20260922.zip --sha256 eb3f037a87c4405b044bc62560776b207bb1c57bd06214262f3cf939658b5b67
```

This batch did not modify board rendering or rerun Chromium acceptance; browser
results in the previous report remain dated prior evidence, not a new run.

## Change List and Preserved Boundaries

- Added: `stock_agent/evidence_excerpts.py`, `scripts/prepare_native_acceptance.py`,
  `tests/test_evidence_excerpts.py`, `tests/test_native_acceptance_bundle.py`,
  `docs/EVIDENCE_EXCERPTS_LOCAL_V1.md`, `docs/NATIVE_ACCEPTANCE_RUNBOOK.md`, this report.
- Updated: `stock_agent/cli.py`, `stock_agent/research_worker.py`,
  `docs/implementation/BASELINE.md`. Existing unrelated UAT CLI changes preserved.
- Generated: the source-only ZIP above. Existing output overwrite is prohibited.
- No true LLM call, network collection, credentials/config/provider/roster change,
  scheduled-task install, source commit, trading action or production approval.
- Tests use temporary vault producers and SignalStore. During testing the guarded
  runner prevented a draft fixture's relative deletion path; the fixture was
  corrected to resolve the object under the temporary project context. No real
  file was deleted. Final vault ledger remains 0 bytes, LastWriteTime
  2026-09-21 16:20:55. No historical event or incident evidence was rewritten.

## Next Dependency Order

1. Execute the prepared native suites in a disposable Windows VM. Record the real
   runtime/dependency/isolation/exit/cleanup evidence; do not weaken the host guard.
2. Add a version-bound derived-summary review workflow with authenticated human
   identity, original excerpt access, rejection/return-for-evidence and stale-version
   invalidation. Treat semantic support and permission for any external summary
   separately from hashes. Source originals stay local.
3. With actual public-source permission, retained original, human identity and
   authorized live-model prerequisites, run W10: one public event -> bound task ->
   Pi draft and explicit unknowns -> human input/acknowledgement -> auditable outcome.
   Synthetic tests cannot substitute for this real research sample.
4. Only after W10 acceptance consider W11 additional parallel roles and branches.
   Keep incident review, institutional approval and business NO_GO separate from
   software test results. No silent conversion of BLOCKED into production GO.
