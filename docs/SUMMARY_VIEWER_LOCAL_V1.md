# Account-Issued Local Summary Viewer

Implemented 2026-09-23 as the next narrow W09 review-access slice. This is a
short-lived, read-only view of ONE existing summary draft. It does not introduce
an approval API, a new orchestrator, a second business ledger, enterprise SSO,
institutional release authority or a permanently running web service.

## Operator Workflow

1. A real operator must already own an active, unique Windows identity binding
   and `summary_review_authorized` in the enforced research roster. This command
   does not create people, bind accounts, approve binding evidence or grant roles.
2. Inspect the current task version and exact draft ID using the existing local
   `task-summary --action show`. Its filesystem/CLI access boundary is unchanged.
3. As the actual authorized operator, run:

```powershell
.\.venv312\Scripts\python.exe -B -m stock_agent task-summary-open --config config/watchlist.local.json --task-id <task-id> --draft-id <draft-id> --expected-version <current-version> --actor <registered-reviewer>
```

4. The CLI authenticates its native process account, verifies the current draft
   and retained evidence, then opens the default browser on an ephemeral
   `127.0.0.1` port. It never prints an access token or writes one to project files.
   No listener is created when account or source validation fails.
5. The page displays task/summary versions, model/replay provenance, author,
   exact retained excerpts, source URLs as TEXT, hashes and local review history.
   Browser access itself is NOT recorded as a review, sign-off or acknowledgement.
6. Close access with the page button, Ctrl+C in the launching CLI, or let its
   absolute five-minute lifetime expire. The CLI then closes its own socket.
7. To decide, use the existing independent-account `task-summary --action review`
   with exact draft/summary IDs and expected version. This page cannot submit a
   decision, reuse its bearer token as approval authority or rerun a model.

There is no shared bookmarkable authenticated URL. Reloading loses access and
requires a new authenticated CLI launch. A version change also requires reopening.
The existing task board on 8768 and workbench on 8767 are neither restarted nor
silently given reviewer authority by this feature.

## Trust Boundary

- Authentication happens ONCE at issuance using `authenticate_local_actor`, not
  the web server's Windows token on each browser request. The claimed actor must
  match the CLI account's native SID and current enforced permissions/coverage.
- The browser then proves possession of a random local viewing capability.
  This is explicitly `local_account_issued_bearer_access_not_browser_identity_or_human_presence`.
  It does NOT identify the person sitting at the browser or establish nonrepudiation.
- A 256-bit random bootstrap value travels in a URL fragment. The empty page
  removes it with `history.replaceState` before one same-origin exchange. It is
  single-use and exchanged for a different random token, retained only in JS
  memory; the server retains only the session token's SHA-256 digest.
- There are no cookies, localStorage/sessionStorage records, remote assets,
  analytics, source-link navigation, CORS allowances or request logs. The exact
  loopback Host including port is required; POST also requires exact Origin and a
  custom header. Browser cross-site requests cannot mint or close a session.
- Responses use no-store, no-referrer, nosniff, frame denial and nonce-based CSP.
  Untrusted titles, summaries, excerpts and notes use `textContent`, never HTML.
- Only `/session`, `/api/detail`, `/close` and the empty `/` shell exist. The
  session chooses neither actor nor task from an HTTP body. Queries, other tasks,
  summary mutations, human feedback and approval routes are unavailable.
- Session lifetime is non-sliding and enforced using a monotonic clock. Each
  authenticated content read opens the existing database read-only and checks
  exact task version, draft validity, verified source bytes, account/policy
  fingerprint and the configuration file digest. Config changes, revocation,
  new task versions or evidence failures permanently invalidate that session.
- The view is pinned to its initial content hash. It cannot silently display a
  different draft while retaining the original operator context.

The page polls every three seconds. It clears displayed content at the next
failed validation/refresh, on explicit close, on pagehide and on its local expiry
timer. Slow responses cannot restore content after closure. This is bounded
revalidation, NOT an instantaneous push revocation guarantee. A suspended browser,
screenshots, extensions or content already read cannot be recalled or erased.

The fragment avoids putting the capability in the HTTP request URL; it is not a
claim that OS URL handlers, process inspection or browser internals never see it.
Someone who steals the capability can read this one view until expiry/revocation.
Processes with the same account authority, administrators, writable roster/code
tampering and an untrusted browser are outside this local account threat model.
This stdlib, loopback-only HTTP server is not an internet-facing production IdP.

## Persistence and Boundaries

No business state is written: no new database tables, receipts, outbox events,
review decisions, credentials, roster rows or governance events. Data is read
through a SQLite read transaction; the command does not migrate a missing/old DB,
load `.env.local`, call LLMs, collect sources or create global directories.

Response projection omits native SIDs, binding-control references and filesystem
paths. It intentionally includes the authorized local draft, excerpts and review
rationale. The existing unauthenticated board's older task projections and
self-reported personal feedback are NOT upgraded into an authenticated system.

The viewer keeps every review `external_eligible=false` and
`institutional_approval=false`. Original bytes do not leave the machine. No
production identity was enrolled, no real review performed and no service or
startup task installed during this implementation.

## Verification

Unit/integration tests use the real vault importer, signal store, draft APIs,
read-only task DB and loopback HTTP in OS temporary directories. Only the native
SID lookup is replaced with synthetic identities; authorization stays enabled.

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_summary_viewer.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py --pattern test_summary_review.py
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
```

Browser driver: `tests/browser_summary_viewer.cjs`, with the existing local
Playwright module/Chromium selected using `PLAYWRIGHT_MODULE` and
`PLAYWRIGHT_CHROMIUM_EXECUTABLE`. It starts guarded locked Python via
`tests/summary_viewer_fixture.py`, creates only synthetic temporary state, captures
desktop/mobile screenshots in `output/playwright/`, then closes its own servers
and cleans its own temporary directories. No real identity acceptance is implied.

The old native acceptance ZIPs remain immutable snapshots of earlier source.
They do NOT cover this viewer. Native PowerShell/process/junction acceptance
remains a separate disposable-Windows requirement, not permission to relax the
host's child-process guard.

## Next Gates

1. Real operator-owned binding records and independent human review of identity
   setup; then local account UAT. Do not manufacture identities to demonstrate GO.
2. Dispose-and-rebuild Windows native acceptance with recorded evidence. Keep its
   source snapshot distinct from current source and from full business acceptance.
3. W10: one real public event through authorized source, retained original,
   governed single Pi/model path, exact citations, authentic human review and
   acknowledgement. No synthetic artifact counts toward that sample.
4. Enterprise browser authentication, protected institutional actions and signed
   identity evidence need the fuller WS20 design and independent security review.

## Design References

The URL-fragment transport follows [MDN's fragment semantics](https://developer.mozilla.org/en-US/docs/Web/URI/Reference/Fragment).
The page uses [MDN's nonce-based CSP mechanisms](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP).
Random identifiers, absolute expiry and revocation follow the relevant guidance
in [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
These references inform controls; they are not a security certification of this
implementation or an authorization to expose the server beyond loopback.
