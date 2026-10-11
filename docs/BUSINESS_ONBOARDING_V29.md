# Business Onboarding v29

## 1. Purpose

v29 turns the existing technical `NO_GO` reasons into an ordered institutional onboarding process. It is for
internal A-share research operations only. It does not create a securities business licence, replace a firm's
legal/compliance review, approve a data licence, publish a research report, or authorize trading.

The system keeps two independent statuses:

- `technical_status=READY`: ledgers, projections, hashes, configuration and required files can be evaluated.
- `business_status=READY`: the current organization, real people, providers, archives, scheduled evidence and
  fingerprint-bound human approvals all satisfy the configured policy.

Technical `READY` is never business `GO`. Missing real evidence remains `BLOCKED`, even when every test passes.

## 2. Regulatory basis and project controls

The operating order is based on the following current official materials:

- The Securities Association of China's research-report practice rules require lawful information sources,
  verification of cited information and data, retained workpapers, pre-release quality control, and separation
  between report production and sales activity:
  <https://www.sac.net.cn/sjb/zlgl/zlgz/202512/t20251231_69839.html>
- The information-barrier guidance applies need-to-know access, public/confidential-side separation, controlled
  wall crossing and confidentiality duties for external service providers:
  <https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69829.html>
- The CSRC information-technology rules require pre-launch internal review and retained records, data lifecycle
  controls, classification and least privilege, supplier due diligence, service/confidentiality agreements, SLAs,
  monitoring, incident response and exit arrangements. Outsourcing does not transfer the institution's duty:
  <https://www.csrc.gov.cn/csrc/c106256/c1653896/content.shtml>
- Research report archives and workpapers are retained for at least five years under the compliance effectiveness
  guidance: <https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69856.html>

The project's two-provider minimum is an internal continuity policy for critical research data and external-model
services. It is not presented as a universal statutory minimum.

## 3. Data boundary

Internal contracts, identity records, qualification certificates, confidentiality undertakings, training records,
security assessments and approval files stay in the institution's approved DMS/records archive. Stock Agent stores
only:

- a `control:<id>` identifier;
- category, title, subject and responsible role;
- external managed archive URI;
- local SHA-256, byte size and media type;
- validity dates;
- independent review decisions and review-record URIs;
- append-only event-chain metadata.

Raw control files are never copied into `data/` or `reports/`. Archive references may use configured `records://`,
`dms://`, `archive://` or non-local `https://` URIs. Usernames, passwords, query strings, fragments, API keys and
local filesystem paths are rejected so temporary access credentials cannot enter the audit ledger.

The registry proves which bytes a human reviewed; it does not prove the document is legally authentic. Authenticity,
authority and archive retention remain human and organizational responsibilities.

## 4. Roles and separation of duties

The current production policy requires seven independently staffable functions:

| Function | Required role | Protected responsibility |
|---|---|---|
| Research coordination | `research_coordinator` | scope, capacity, escalation and operating readiness |
| Research decision | `lead_analyst` | research-readiness approval |
| Independent quality | `quality_reviewer` | quality-readiness approval |
| Compliance | `compliance_officer` | public-information, conflict and disclosure review |
| Information security | `information_security_officer` | data egress, supplier security and archive review |
| Data governance | `data_steward` | data rights, provider due diligence and controlled activation |
| Internal publication | `publication_approver` | final internal-research activation |

Each of the six production approval gates requires a different active human actor. The publication approver who
approved publication readiness performs final activation. LLM, Pi, Agent, service and system identities cannot
approve or activate. An actor used for a protected pilot gate must also be onboarding-ready for that role.

Control-evidence registration and review form a bootstrap root of trust. Registration still requires an active
rostered human. A reviewer must be independent from the preparer and evidence subject, have the configured role and
authorization, and required multi-role reviews must use different people. Those bootstrap rights do not create
business `GO` or production activation.

## 5. Eight-stage operating order

### Stage 1: Governance scope

1. Obtain the real legal-entity identifier used by the institution.
2. Archive and register the research mandate, internal-research scope and prohibited-actions policy.
3. Obtain the category-specific independent reviews.
4. Set `business_onboarding.organization_id` only to the real institution identifier.

Placeholders, `pending` values and organization evidence belonging to another subject remain blocked.

### Stage 2: Human roster

1. Start from `config/research_roster.example.json` and create the local roster outside source control.
2. Enter real active humans, actual roles, information side, coverage, restrictions and authorizations.
3. Register employment, confidentiality, training and role-appointment evidence for every protected actor.
4. Register qualification evidence for the lead analyst and quality reviewer.
5. Make the analyst qualification `evidence_ref` equal the verified `human_qualification` control ID.
6. Confirm seven roles can be assigned to distinct onboarding-ready humans.

Do not invent names, registration numbers, authorizations or qualification evidence to clear the gate.

### Stage 3: Provider authorization

For each provider, complete the following in order:

1. Candidate intake: legal entity and requested scopes only, no approval.
2. Data-steward due diligence: legal contract, data rights/provenance, confidentiality, security assessment, data
   dictionary, timestamp semantics, SLA, incident response, continuity/exit and external-model-use terms.
3. Independent compliance review.
4. Independent information-security review by another human.
5. Manual activation by a data steward or research coordinator who was not a reviewer.

Every due-diligence field must point to a currently verified provider control-evidence ID for that provider. Expiry,
rejection or revocation immediately removes an otherwise active provider from the usable set. The pilot requires two
active independent providers under current project policy.

### Stage 4: Official evidence backfill

For every P0/P1 workpaper, locate the exact exchange, CNInfo, regulator or issuer original. Import and independently
verify the original in the official evidence vault, then link its `vault_document_id` to each affected signal and
workpaper. Aggregators, screenshots and news summaries cannot impersonate official originals.

### Stage 5: Governance remediation

Clear market-data authorization/freshness/conflict failures and LLM classification/citation/exception failures without
weakening enforcement. Governance exceptions are handled only by authorized compliance or information-security
humans and do not trigger automatic retransmission.

### Stage 6: Production archive

Complete and independently review:

- production archive contract;
- information-security and access controls;
- retention policy and evidence;
- official-evidence vault archive reviews and activation;
- research-workpaper production archive approval.

The local content-addressed vault remains a shadow evidence store until an approved external immutable archive is
contracted, reviewed and manually activated.

### Stage 7: Shadow readiness

Only after preflight is ready, collect genuine Windows Task Scheduler daily runs. A qualifying run must:

- use `trigger_source=scheduled`;
- complete the configured daily chain exactly once;
- reconcile the scheduled summary with the current report and public handoff;
- have no disqualifying governance, model, incident or review-capacity state;
- belong to the current candidate fingerprint.

Manual reruns, repeated runs for quota, stale reports and runs from an old fingerprint do not count.

### Stage 8: Go/No-Go

1. Synchronize the pilot and copy the current candidate fingerprint.
2. Six different onboarding-ready humans review the same fingerprint and record their own gate decisions.
3. Each approval references a verified `approval_support` record whose subject is that fingerprint, or an approved
   managed archive URI.
4. After all other gates pass, the same publication approver performs manual internal-research activation.
5. Any critical configuration, roster, provider or control-evidence ledger change creates a new fingerprint and makes
   old approvals/activation inapplicable.

`GO` only enables controlled internal research operations. It does not enable client publication or trading.

## 6. Operator commands

Generate templates without changing production state:

```powershell
python -m stock_agent business-onboarding-pack --config config/watchlist.local.json
```

Register one externally archived control document. Use a real local file only for hashing; it is not copied:

```powershell
python -m stock_agent control-evidence-register --config config/watchlist.local.json `
  --file <local-review-copy> --category <category> --title <title> --subject-id <subject> `
  --owner-role <preparer-role> --external-archive-ref <records://managed/object> `
  --actor <human-actor> --actor-role <preparer-role> --valid-to <ISO-time> --notes <metadata>
```

Record an independent review:

```powershell
python -m stock_agent control-evidence-review --config config/watchlist.local.json `
  --evidence-id <control:id> --decision approve --actor <independent-human> `
  --actor-role <required-role> --notes <review-conclusion> `
  --review-record-ref <records://managed/review-record>
```

Fail closed when evidence is withdrawn:

```powershell
python -m stock_agent control-evidence-revoke --config config/watchlist.local.json `
  --evidence-id <control:id> --actor <authorized-human> --actor-role <role> --notes <reason>
```

Provider onboarding uses `provider-init`, `provider-due-diligence`, two `provider-review` calls and then
`provider-activate`. The due-diligence JSON file contains all required field-to-`control:<id>` mappings and no raw
contracts or credentials.

Synchronize and inspect the ordered queue:

```powershell
python -m stock_agent control-evidence-status --config config/watchlist.local.json --require-ready
python -m stock_agent business-onboarding-sync --config config/watchlist.local.json
python -m stock_agent business-onboarding-status --config config/watchlist.local.json
python -m stock_agent production-pilot-sync --config config/watchlist.local.json
python -m stock_agent production-pilot-status --config config/watchlist.local.json
python -m stock_agent doctor --config config/watchlist.local.json
```

`business-onboarding-status --require-ready` and `production-pilot-status --require-go` intentionally return non-zero
until the real human-controlled prerequisites are complete.

## 7. Scheduled operations and autostart

The enforced daily order is:

```text
market-cache-seed
-> agent-run
-> evidence-vault-status --require-ready
-> public agent handoff / conditional isolated Pi call
-> review-queue-sync
-> research-ops-plan
-> workspace-sync
-> production-pilot-sync
-> business-onboarding-sync
-> dashboard
-> alerts
```

Do not install or enable Windows scheduled tasks while Business Onboarding or Production Pilot is blocked. Preview
task definitions with the installer's `-DryRun` mode. v29 does not install tasks and does not create an autostart
entry. The user must explicitly authorize a later real installation after business `GO`.

## 8. Evidence and outputs

- Append-only control ledger: `data/business_onboarding/control_evidence_events.jsonl`
- Control registry projection: `reports/business_onboarding/control_evidence_latest.json`
- Ordered onboarding status: `reports/business_onboarding/latest.json`
- Human-readable status: `reports/business_onboarding/latest.md`
- Template-only intake pack: `reports/business_onboarding/intake_pack/`
- Pilot fingerprint and Go/No-Go: `reports/production_pilot/latest.json`
- Scheduled run evidence: `logs/scheduled/run_*.summary.json`

Do not edit append-only ledgers. Correct a mistake with a later review, rejection, revocation or superseding evidence
event. Do not delete historical reports, approvals or incident records to improve status.

## 9. Acceptance criteria

Technical acceptance:

```powershell
python -m unittest discover -s tests
python -m py_compile stock_agent/control_evidence.py stock_agent/business_onboarding.py
python -m stock_agent control-evidence-status --config config/watchlist.local.json --require-ready
python -m stock_agent business-onboarding-sync --config config/watchlist.local.json
python -m stock_agent doctor --config config/watchlist.local.json
```

Business acceptance requires external evidence that automation cannot create:

- real legal entity and approved internal research mandate;
- real human roster, qualifications, authorizations and duties;
- current provider contracts, data rights, security and resilience reviews;
- approved immutable production archives and retention evidence;
- cleared official-original, market-data and LLM governance queues;
- genuine scheduled shadow runs over the configured trading days;
- six distinct human approvals and manual activation for the current fingerprint.

Until those are complete, the correct commercial result is `technical_status=READY`,
`business_status=BLOCKED`, `go_live_decision=NO_GO`.
