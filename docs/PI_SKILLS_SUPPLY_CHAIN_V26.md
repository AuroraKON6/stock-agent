# Pi Skills Supply Chain v26

## 1. Objective

Add useful external research methods without turning GitHub content into a new
execution or data-egress surface. Production Pi remains a no-tool, no-session,
no-trust LLM harness. Python validates and compiles reviewed method text into
fixed role prompts.

This layer does not authorize a data vendor, fetch a filing, approve research,
publish output, assign staff, or execute a portfolio action.

## 2. Candidate Review

All accepted source revisions are full Git commits. Repository snapshots under
`output/skill_audit/sources_20260823/` are audit working copies, not runtime
dependencies.

| Repository | Pinned commit | License | Useful methods | Decision |
|---|---|---|---|---|
| `nexaforgelab/a-share-investment-skill-pack` | `fe9ec77192e17fd638e7cd61a6254289f9193b3b` | Apache-2.0 | A-share data quality, conflict handling, disclosures, inquiry letters, financial quality, counter-argument, review learning | Adapt selected methods only |
| `anthropics/financial-services` | `33a3d8a9d6e5c3d4861731933a8857cc5e03315d` | Apache-2.0 | Falsifiable thesis ledger, catalyst outcomes, sector and competitor mapping | Adapt selected methods only |
| `rollingSirius/equity-research-skill` | `3d94e64ff53b325d866ea4bff69ad81a5adca8f5` | MIT | A-share source hierarchy, point-in-time evidence, conflict reconciliation, forensic checks | Adapt evidence methods only |
| `pppop00/Equity-Research-Company` | `bc8663b508569e9716c113626c10523d3230cd1d` | Apache-2.0 | Append-only incident learning, numeric and narrative adversarial review | Adapt incident method only |
| `OctagonAI/skills` | `51e938c4d086f658de8bdcf734e864d34637167e` | MIT | SEC analysis ideas | Rejected: external MCP/API requirement and trading automation in the pack |
| `counterposition/pi` | reviewed 2026-08-23 | mixed project code | Agent runtime patterns | Rejected: broad orchestration and execution surface |
| `mariozechner/pi-mono` | reviewed 2026-08-23 | upstream runtime | Pi implementation reference | Runtime reference only, not a business skill |

Whole repositories were not installed. Position sizing, Kelly sizing, target
prices, stop losses, pre-positioning, trade execution, downloaders, MCP clients,
shell commands, and package installers were excluded.

## 3. Approved Local Methods

The registry currently controls 11 methods: five existing project-owned methods
and six new adaptations.

| New method | Production purpose |
|---|---|
| `source-provenance-audit` | Source authority, point-in-time freshness, traceability and unresolved conflicts |
| `a-share-disclosure-analysis` | Announcements, periodic reports, inquiry letters, responses and amendments |
| `financial-quality-review` | Cash conversion, working capital, accounting scope and evidence-backed risk questions |
| `thesis-catalyst-ledger` | Falsifiable hypotheses, evidence for/against, catalyst expectation versus outcome |
| `sector-competitive-map` | Dated sector scope, value chain and comparable competitive evidence |
| `incident-learning-review` | Human-confirmed incidents, independent numeric/narrative challenge and controlled improvements |

Exact role mappings and provenance are in `config/pi_skills.json`. Exact file and
role-bundle hashes are in `config/pi_skills.lock.json`.

## 4. Runtime Architecture

```text
GitHub candidate
  -> license / commit / permission / content review
  -> project-owned .pi/skills/<id>/SKILL.md
  -> config/pi_skills.json provenance registry
  -> explicit pi-skills-lock --write
  -> config/pi_skills.lock.json
  -> per-call lock verification
  -> role-specific prompt compilation
  -> isolated Pi process with --no-skills --no-tools
  -> hash-chained call audit
```

The apparent contradiction is intentional: Pi's dynamic skill loader remains
disabled. The local `SKILL.md` files are reviewed text inputs owned by Python,
not executable Pi plugins.

Any change to a method, source pin, permission, registry, or role mapping causes
a lock mismatch before model execution. Registry and lock hashes are also
production-pilot critical inputs, so old fingerprint-bound approvals cannot be
reused after a method change.

## 5. Controls

- Paths must remain under `.pi/skills/<id>/SKILL.md` with no traversal.
- Adapted methods require a canonical GitHub URL, allowed license, source paths,
  and a full 40-character commit.
- Network, shell, filesystem write, trading, approval, publication, assignment,
  and configuration mutation permissions must all be false.
- Executable blocks, installer commands, prompt-override phrases, MCP call names,
  and order-submission markers fail validation.
- Skill and role-bundle byte limits prevent uncontrolled prompt growth.
- The base role contract and output schema remain authoritative.
- Call audit stores hashes and skill IDs, not raw business input or credentials.

## 6. Operations

Read-only status:

```powershell
python -m stock_agent pi-skills-status --config config/watchlist.local.json --require-ready
python -m stock_agent pi-runtime-status --config config/watchlist.local.json --require-ready
```

After a reviewed change, explicitly regenerate the lock:

```powershell
python -m stock_agent pi-skills-lock --config config/watchlist.local.json --write
```

One billable, synthetic public UAT through the locked signal-analyst bundle:

```powershell
python -m stock_agent pi-skills-uat --config config/watchlist.local.json --live
```

The UAT uses conflicting synthetic values, requires human control, and validates
that no trade was executed. It contains no issuer, client, account, position, or
internal data.

## 7. Update Procedure

1. Clone or fetch the candidate only into an audit workspace.
2. Record canonical repository URL, full commit, license, and exact source paths.
3. Review data rights, required tools, network calls, secret handling, prompt
   injection, and prohibited business actions.
4. Rewrite only the necessary method as a concise local no-tool skill.
5. Add or update the registry and third-party notice.
6. Run focused supply-chain tests before writing a new lock.
7. Review the diff, then run `pi-skills-lock --write` explicitly.
8. Run full tests, status, Doctor, Dashboard and the synthetic live UAT.
9. Treat the changed production fingerprint as a new candidate. Human approvals
   are not carried forward.

Never use a branch name, release tag, latest URL, package installer, or remote
skill loader as the production pin.
