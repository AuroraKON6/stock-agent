# Disposable Windows Native Acceptance

This procedure is for a disposable Windows VM, NOT the business workstation.
The bundle builder and verifier do not launch tests, provision a VM, install
tasks, download software, or certify OS isolation. A ZIP is neither encrypted nor
a sandbox. PREPARED / VERIFIED never mean native acceptance passed.

## Host Preparation (Read Source Only)

Use the locked interpreter with bytecode writes disabled:

```powershell
.\.venv312\Scripts\python.exe -B scripts/prepare_native_acceptance.py --output reports/native_acceptance.zip
.\.venv312\Scripts\python.exe -B scripts/prepare_native_acceptance.py --verify reports/native_acceptance.zip --sha256 <separately-recorded-digest>
```

An existing output is refused. The allowlist includes Python application sources,
three specific test files and the PowerShell scripts those tests inspect, this
runbook, AGENTS.md and dependency locks. All included file hashes are recorded.
No real config, .env, credential profile, .git, venv, skills, data, logs or reports
are copied. A new empty, no-source, model-disabled local config is generated.
Credential-like source literals block packaging with redacted diagnostics; this
scan is not a guarantee that arbitrary source files contain no sensitive data.
Review the manifest before transfer. Keep the ZIP digest separately from the ZIP.

## Guest Prerequisites

1. Use a fresh disposable Windows VM with a local non-business user. Do not map
   production drives, host profiles, network shares, credentials or clipboard.
2. Provision an independently reviewed Python 3.12 installation and the exact
   `requirements.lock.txt` dependencies from a trusted offline wheelhouse. Create
   `.venv312` INSIDE the extracted workspace. Do not copy the host venv: its base
   interpreter path may point to a host cache. Do not edit the lock to get green.
3. Block guest outbound networking outside the guest process boundary; loopback
   must remain usable. Record this control and the VM image/snapshot identity.
   No API keys or logged-in Pi/OpenClaw profiles may exist in the guest.
4. Transfer only the reviewed bundle and verified offline runtime dependencies.
   Validate the entire ZIP digest before extraction into an empty local directory.
   Then run the bundled verifier on the original ZIP with the separately recorded
   digest. Inspect that all manifest paths are inside the workspace and the local
   config is synthetic. This is byte-integrity evidence, not trust in unknown code.
5. A Windows Sandbox or container that cannot support the required native process
   APIs must not silently substitute for the declared Windows VM acceptance.

## Guest Execution Only

Run from the disposable extracted workspace, after the prerequisites above:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:Path = (Join-Path $PWD '.venv312\Scripts') + ';' + $env:Path
.\.venv312\Scripts\python.exe -B --version
.\.venv312\Scripts\python.exe -B -m unittest discover -s tests -p test_research_workbench_ops.py -v
.\.venv312\Scripts\python.exe -B -m unittest discover -s tests -p test_z002_run_data_boundaries.py -v
.\.venv312\Scripts\python.exe -B -m unittest discover -s tests -p test_z003_run_identity_recovery.py -v
```

Capture each command's stdout, stderr and exit code independently, including every
skip and its reason. The lifecycle test starts a hidden loopback-only workbench
with an ephemeral port and tests idempotent start, health, stop and repeated stop.
Reused-PID tests must not kill an unrelated sleeping test process. Junction tests
must reject manifest paths through junctions. Shortcut tests may skip without the
optional pywin32 dependency; a skip is an open acceptance item, not a pass. Do not
install an unlocked dependency or run create-shortcut scripts on the host.

The bundle's scheduled scripts are parsed, NOT executed. Do not install scheduled
tasks, run `agent-run`, enable providers or call models for native acceptance.
After each command, check for owned test processes still running inside the VM.
Stop only the exact owned process or discard the entire VM; never terminate host
processes by name. Preserve failure diagnostics before discarding the guest.

## Required Evidence and Status

Record bundle digest and file manifest; VM image/snapshot; interpreter version;
dependency-lock hash and installed-version comparison; network-isolation evidence;
per-command exit codes, output, tests run, failures, errors and skips; process
cleanup; generated file paths confined to guest/temp; and reviewer identity.

- A failed assertion is FAIL.
- Missing runtime/OS isolation, guard denial, unexecuted or skipped required tests
  remain BLOCKED / NOT_RUN, with reasons. Never relabel them PASS.
- Even all native checks passing establishes only these technical behaviors.
  Human identities, provider permission, original-document incident review,
  live-model evidence, business Go/No-Go and production enablement stay separate.

Host offline tests continue through `scripts/run_isolated_tests.py`, whose child
process guard stays enabled. Do not add a bypass flag to make native tests green.
