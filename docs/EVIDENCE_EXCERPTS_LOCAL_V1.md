# Local Evidence Excerpts and Summary Drafts

## Purpose and Boundary

Bind a local research excerpt to the exact retained original, without treating a
document title or an arbitrary summary as verified body evidence. This is an
offline read-only tool, NOT a publication approval or permission to send document
bodies to a model. It does not fetch, call Pi/LLM, add a vault event or approve a
provider. The original vault incident still requires independent human review.

## CLI

Use a real retained document ID and offsets from its text. These commands show
syntax only; the placeholder ID is not a real research sample:

```powershell
.\.venv312\Scripts\python.exe -B -m stock_agent evidence-excerpt --config config/watchlist.local.json --document-id <document-id> --start 0 --end 100
.\.venv312\Scripts\python.exe -B -m stock_agent evidence-excerpt --config config/watchlist.local.json --document-id <document-id> --start 0 --end 100 --summary "Unreviewed local interpretation" --summary-kind human_judgment
```

JSON is written to stdout only. It includes the original excerpt, a reusable
selector and, if requested, an unapproved summary draft. Do not forward stdout to
an external model or treat it as an approved archive record. Invalid ranges or
unverified documents exit with an error, never a substituted title or excerpt.

## Extraction Contract

- The authoritative vault verifier must report technical READY. Objects must
  exist and their recorded size, byte hash and event chain must verify. This is
  integrity verification, not certification of a document's claims or authority.
- Supported format: retained `text/plain`, strict UTF-8, optional leading BOM.
  Maximum source size is 2 MiB; each excerpt is at most 1,200 Unicode code points.
  HTML, PDF, binary/invalid UTF-8 and non-text control characters fail closed.
  There is no implicit decoder fallback, HTML stripping, OCR or PDF parser.
- Offsets are `[start, end)` Unicode code points after removing a leading UTF-8
  BOM only. CRLF, spaces and other text are not normalized. Line numbers count LF
  boundaries for display; the offsets and hashes are the authoritative locator.
- Reopen the canonical content-addressed object and hash the bytes actually
  decoded. A stale successful vault check cannot excuse later byte tampering.
  Noncanonical paths and symlink/junction parents are rejected. These checks do
  not turn the local filesystem into an adversarial OS security sandbox.
- Selectors contain document ID, original-byte SHA-256, extractor version,
  decoded-source SHA-256, start/end and excerpt SHA-256. Every use reconstructs
  the excerpt and compares the whole selector. Changed bytes, versions, ranges,
  document IDs, missing or extra selector fields are rejected.

## Summary Provenance, Not Truth Certification

`bind_summary_draft` accepts one to five verified selectors and a nonempty draft
up to 4,000 characters. `content_kind` must be `human_judgment` or
`model_inference`, not `source_statement`. It hashes the draft, content kind and
source excerpt identities into a deterministic version ID.

This proves which exact excerpts a draft cites, NOT that they support its claims.
Every draft retains `review_status=unreviewed`,
`semantic_support_verified=false`, `scope=local_review_only` and
`external_eligible=false`. Calling a draft `human_judgment` does not authenticate
an author. No reviewer identity, semantic approval, sign-off or publish action is
fabricated. A modified draft gets a different ID and has no inherited approval.

## Task Worker Integration

A task evidence entry can carry `source_selection` using the returned selector.
Existing public classification, stored-signal binding, symbol/freshness checks
and retained-document association still apply. A selector cannot bind an unrelated
document to a task. A supplied but invalid selector drops that evidence instead
of silently falling back to title-only delivery.

The worker stores successful local extraction records in
`model_call.context_delivery.local_excerpt_records`. They are NOT included in
`public_context` or the outbound prompt. The externally eligible evidence still
contains title metadata only, explicitly labeled
`summary_scope=title_metadata_only_not_body_analysis`. Its summary hash covers
the actual delivered title, including the existing 1,000-character bound.
This release does not claim body-grounded LLM research or an approved summary
publication workflow. The board UI has not gained an excerpt editor in this batch.

## Acceptance and Next Work

Tests use temporary real SignalStore and vault-importer fixtures, not a fabricated
vault index. Coverage includes exact Unicode/BOM/CRLF offsets; bounds; bad encoding
and size; original tampering/deletion; selector tampering and cross-document
rebinding; immutable read-only behavior; summary versioning; CLI output; local
worker receipt; outbound omission; and delivered-title hash consistency.

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_evidence_excerpts.py
```

Next: a version-bound human review workflow for derived summaries, with an
authenticated independent reviewer and revocation/expiry semantics. Any later
external derived-summary delivery needs explicit source-use permission, renewed
governance checks and a separately reviewed contract; this local tool does not
enable it. PDF/HTML support requires a pinned, reviewed extraction toolchain and
format-specific golden fixtures before use on real announcements.
