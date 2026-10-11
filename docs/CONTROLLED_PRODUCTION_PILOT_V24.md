# Controlled Production Pilot v24

## Purpose

This release turns a technically working research pipeline into a controlled internal-research pilot. It does not authorize investment advice, external publication, trading, orders, rebalancing, or position changes.

The system has three deliberately separate states:

1. `technical_readiness`: commands, artifacts, model telemetry, schedules, and audit logs work.
2. `candidate_readiness`: data rights, human capacity, governance exceptions, incidents, shadow evidence, and independent approvals are complete.
3. `go_live_decision`: a fingerprint-bound human activation exists and all gates still pass.

An `OK` or `WARN` Doctor result is never a business authorization. The effective mode remains `shadow` whenever a mandatory gate fails.

## Control Basis

The controls are engineering and operating benchmarks, not a claim that this project or its owner is a regulated securities institution. Applicability must be confirmed by authorized legal and compliance personnel.

- The CSRC securities-fund IT management measures require internal governance, risk management, pre-launch review, retained records, monitoring, audits, service-provider due diligence, contracts, confidentiality, service-quality monitoring, incident/change/termination arrangements, and replacement planning: https://www.csrc.gov.cn/csrc/c106256/c1653896/content.shtml
- The CSRC IT security measures require reliable architecture and capacity, tested changes, backup and disaster recovery, monitoring and incident response, and assessment of supplier qualification, experience, service quality, contracts, and confidentiality: https://www.csrc.gov.cn/csrc/c106256/c1653967/content.shtml
- The CSRC continuity guide uses business-impact analysis, RTO/RPO, continuity governance, approved plans, and incident-response organization: https://www.csrc.gov.cn/csrc/c101954/c6550642/6550642/files/%E9%99%84%E4%BB%B64%EF%BC%9A%E8%AF%81%E5%88%B8%E6%9C%9F%E8%B4%A7%E4%B8%9A%E4%BF%A1%E6%81%AF%E6%8A%80%E6%9C%AF%E6%9C%8D%E5%8A%A1%E8%BF%9E%E7%BB%AD%E6%80%A7%E7%AE%A1%E7%90%86%E6%8C%87%E5%8D%97.pdf
- The Securities Association of China compliance-effectiveness guidance calls for retained research-source evidence, named analyst responsibility, independent quality review, compliance review, publication audit trails, and market-impact controls: https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69856.html

## Daily Operating Order

### 08:30 data and research plane

1. Refresh the market cache.
2. Run exactly one `agent-run`.
3. Run `evidence-vault-status --require-ready`; a technical object or event-chain failure stops external handoff.
4. Generate a hash-bound, public-only `agent-handoff` snapshot from the current JSON report.
5. Invoke isolated Pi only when that snapshot contains at least one governance-approved public signal. Zero eligible signals means zero Pi calls.
6. Project the deduplicated human review queue.
7. Project research operations and SLA escalations.
8. Build the object workspace.
9. Write a preliminary pilot snapshot.
10. Generate the dashboard and alerts.
11. Close the scheduled-run summary with eligible-signal, invocation, model-call count, and skip-reason fields.

The Pi runtime has no dynamic tools, sessions, extensions, or skills. Its payload excludes internal rosters, queues, provider decisions, governance exceptions, local paths, secrets, raw evidence bytes, and private model fields. Signal text remains untrusted data.

### 08:45 independent control plane

1. Read the completed 08:30 scheduled summary.
2. Hash the config, roster, restrictions, source report, public handoff, model record, governance records, queue, runtime, workspace, dashboard, and alerts.
3. Reconcile the input/output manifest, official-evidence report, handoff source-report hash, scheduled signal count, conditional Pi invocation, and command order; append one idempotent run-ledger record.
4. Count the run only when every qualifying-shadow condition passes.
5. Refresh the dashboard and alerts without collecting data or calling an LLM.

The 18:00 dashboard task and weekly review task also refresh projections without repeating the daily LLM pipeline.

## Real Human RACI

The roster must contain active, verified humans. One person may not satisfy multiple fingerprint-bound Go/No-Go gates.

| Role | Accountable action | Required evidence/authorization |
|---|---|---|
| research_coordinator | operating calendar, escalation, manual assignment | active human, public information side |
| lead_analyst | research readiness and review execution | verified analyst registration |
| quality_reviewer | independent quality readiness | verified analyst registration; distinct actor |
| compliance_officer | information boundary and compliance readiness | `compliance_authorized` |
| information_security_officer | external model and supplier security readiness | `information_security_authorized` |
| data_steward | data rights, definitions, supplier operations | `vendor_data_steward_authorized` |
| publication_approver | internal-research activation | `publication_authorized` |

Never invent names, registration IDs, roles, authorizations, or acceptance records to clear a gate.

## Provider Onboarding

Use at least two independent quote providers for the production candidate. A static provider name in watchlist config is not approval when pilot enforcement is enabled.

1. Create a candidate. This grants no approval.
2. A real data steward or research coordinator submits every field in `config/provider_evidence.example.json`.
3. A compliance officer independently reviews data rights, permitted research use, confidentiality, and external-LLM use.
4. A different information-security officer reviews architecture, access, secrets, retention, incident handling, and continuity/exit plans.
5. A data steward or coordinator who is not either reviewer activates the provider.
6. Any authorized control role may immediately suspend an active provider. Suspension removes it from LLM-eligible market data without deleting history.

Example command shape, with real values supplied by authorized humans:

```powershell
python -m stock_agent provider-init --provider-id <id> --legal-name <legal-name> --scope internal_research --scope external_llm
python -m stock_agent provider-due-diligence --provider-id <id> --actor <actor-id> --actor-role data_steward --evidence-file <completed-json> --notes <assessment>
python -m stock_agent provider-review --provider-id <id> --decision approve --actor <actor-id> --actor-role compliance_officer --notes <review> --evidence-ref <ref>
python -m stock_agent provider-review --provider-id <id> --decision approve --actor <different-actor-id> --actor-role information_security_officer --notes <review> --evidence-ref <ref>
python -m stock_agent provider-activate --provider-id <id> --actor <actor-id> --actor-role data_steward --notes <activation-record>
```

## Review Backlog Entry Criteria

The current queue is projected by `signal_id`; raw pending/completed/action files remain immutable audit history.

Before a shadow run can qualify:

- at least one verified lead analyst is available;
- no P0/P1 item is unassigned or overdue;
- no review item is overdue;
- WIP is bounded by `review_wip_limit_per_human`;
- batches are suggestions only and require human acceptance.

The pilot report writes deterministic batch suggestions. It never assigns work.

## Shadow Exit Criteria

The default candidate requires all of the following:

- 2 active, evidence-approved providers with `internal_research` and `external_llm` scopes;
- no market-data governance block;
- no pending LLM-governance exception;
- model operations with no breach and retained live-call evidence;
- a complete real human roster with role separation;
- review backlog within the operating limits above;
- zero open critical or high incident;
- 5 qualifying reconciled runs over at least 3 trading days;
- 0 unreconciled runs;
- six distinct fingerprint-bound human approvals;
- one manual publication-approver activation.

Any config, roster, restriction, provider approval, or provider activation change produces a new candidate fingerprint. Old approvals and activation no longer apply.

## Incident Order

1. A registered service or human records an incident with severity, category, notes, and evidence reference.
2. Critical/high incidents immediately block Go-Live.
3. A real compliance or information-security actor acknowledges and mitigates.
4. Critical/high resolution needs both roles and two distinct humans. The reporter cannot independently resolve the incident.
5. Records are appended to the hash-chained incident ledger; no incident history is deleted.
6. Revoking production activation is always fail-closed and never triggers a trade, publication, or data rewrite.

## Go/No-Go Commands

```powershell
python -m stock_agent production-pilot-sync --config config/watchlist.local.json
python -m stock_agent production-pilot-status --config config/watchlist.local.json
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/go_live_check.ps1
```

`go_live_check.ps1` intentionally exits `3` for a technically healthy `NO_GO` candidate and `1` for a technical/control failure. It does not run the LLM pipeline.

## Audit Artifacts

- `data/production_pilot/vendor_events.jsonl`
- `data/production_pilot/control_events.jsonl`
- `data/production_pilot/incident_events.jsonl`
- `data/production_pilot/run_ledger.jsonl`
- `reports/production_pilot/provider_registry_latest.json`
- `reports/production_pilot/latest.json`
- `reports/production_pilot/latest.md`
- `logs/go_live/go_live_*.summary.json`

All four JSONL files are append-only and hash chained. An integrity failure blocks append and Go-Live until an authorized human-led investigation repairs or formally supersedes the ledger.
