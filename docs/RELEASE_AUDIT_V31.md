# Release Audit v1

## 1. Purpose

`release-audit` creates a canonical, reviewable identity for the exact local
source, configuration, dependency locks, Pi runtime policy, model selection,
and existing status projections that would be used by the research system.

It solves one question:

> Which reviewed code, configuration, policy inputs, dependencies, and model
> settings produced or would produce this run?

It does not answer whether an institution has approved production use. That
remains a separate human and evidence-controlled decision.

## 2. Command

```powershell
python -m stock_agent release-audit --config config/watchlist.local.json
```

Strict CI or operator check:

```powershell
python -m stock_agent release-audit --config config/watchlist.local.json --require-ready
```

Normal mode always writes the audit and exits normally so blockers can be
inspected. `--require-ready` exits with code 2 when the technical baseline is
not ready.

## 3. Outputs

- `reports/release_audit/latest.json`
- `reports/release_audit/latest.md`
- `reports/release_audit/release_audit_<timestamp>.json`
- `reports/release_audit/release_audit_<timestamp>.md`

The JSON contains a `manifest_id`, calculated from the stable release identity
projection. Generation time and credential-presence booleans are excluded from
that identity, so the same source and inputs produce the same manifest ID.

## 4. Technical Release Gates

Technical status is `READY` only when all of these are true:

1. A Git repository is inspectable and has a valid `HEAD`.
2. The worktree is clean.
3. `pyproject.toml`, runtime config, research roster, and restriction policy exist.
4. A supported Python dependency lock exists.
5. When Pi is enabled, its package, package lock, isolated production settings,
   and configured version agree.
6. When compiled Pi methods are enabled, the reviewed registry and lock validate.
7. Secret scan and CycloneDX SBOM artifacts are valid and report `PASS`.
8. The license report reports `PASS`; copyleft, compound, denied, or unknown
   expressions remain blocked until a real legal/compliance review is recorded.
9. The Python environment audit proves that both Python 3.11 and 3.12 match the
   same lock, pass smoke imports, and pass the complete test suite. Secret-scan
   and environment evidence must also match the current release-candidate
   fingerprint; a previously passing but stale artifact is blocking.

Supported Python lock names are:

- `requirements.lock`
- `requirements.lock.txt`
- `uv.lock`
- `poetry.lock`
- `Pipfile.lock`

## 5. Pi Boundary

The audit validates the production `pi_runtime/agent_home/settings.json`, not
the interactive developer `.pi/settings.json`, as the execution boundary. The
production settings must keep project trust denied and tools, packages,
extensions, skills, prompts, themes, compaction, retry, analytics, and install
telemetry disabled.

Compiled research methods are validated through `config/pi_skills.json` and
`config/pi_skills.lock.json`. They are method text compiled by Python into a
fixed prompt. Pi dynamic skill discovery remains disabled in production.

## 6. Credential Safety

The report records only:

- expected credential environment variable name;
- whether it is present in the current process;
- `credential_value_recorded=false`.

It never serializes a credential value. Endpoint URLs are normalized to remove
userinfo, query strings, and fragments. A missing key is a runtime warning, not
a reason to weaken source-release controls.

Any DeepSeek key previously exposed in chat or logs must be rotated outside the
repository before production use.

## 7. Freshness

Existing `latest.json` projections are fingerprinted and classified by file
modification age:

| Age | Classification |
|---:|---|
| up to 24 hours | `CURRENT` |
| over 24 and up to 72 hours | `AGING` |
| over 72 hours | `STALE` |
| future timestamp | `FUTURE` |
| absent | `MISSING` |

Stale or missing business projections are warnings in the source release
audit. They are not silently treated as current and cannot become an approval.
Business workflows may impose stricter freshness gates.

## 8. Business Separation

The audit always reports business enablement as `NOT_ASSERTED`. It may display
the scalar status reported by the latest production-pilot and onboarding
projections, but it never transforms those values into an approval.

The following remain outside this command:

- provider due diligence and activation;
- production archive review and activation;
- compliance and information-security decisions;
- human case quality, compliance, and publication approval;
- scheduled task installation;
- data collection or model invocation;
- publication, trading, order placement, or portfolio changes.

## 9. Operation Contract

The JSON records these enforced properties:

```json
{
  "report_write_only": true,
  "network_access": false,
  "market_data_collection": false,
  "llm_call": false,
  "business_state_write": false,
  "approval_or_activation": false,
  "scheduled_task_change": false,
  "trading_or_order_action": false
}
```

## 10. Current Expected Result

The current expected result remains `BLOCKED` until a real source baseline and
all G0 controls are approved. The dependency lock, redacted secret scan,
CycloneDX SBOM, and Python 3.11/3.12 environment evidence now exist. A local
directory with no valid Git `HEAD`, an unclean worktree, or a license report
that still requires human review must not claim release readiness.

This is a technical control result only. Real people, provider contracts,
official source originals, external archive evidence, and independent human
approvals remain separate `BLOCKED` or `NO_GO` gates until supplied and reviewed.
