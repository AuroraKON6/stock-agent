"""Kimi data-source integration (offline-safe candidate, disabled by default).

Importing this package has no side effects: no configuration reads, no
credential discovery, no network, no subprocesses, no installs (plan T01).
The modules are:

- ``contracts``: frozen contract snapshots and typed request validation.
- ``transport``: fixed-origin, credential-explicit gateway transport with
  classified outcomes.
- ``artifacts``: host-decided artifact planning and atomic commits.
- ``client``: the governed end-to-end query flow and ledger records.

Live use requires an explicit ``kimi_integration.enabled`` configuration,
an approved source registration, and a real credential provider; all of
those remain BLOCKED pending the K01 rights matrix.
"""
from .artifacts import (
    ArtifactLimitError,
    ArtifactLimits,
    ArtifactPlan,
    commit_artifacts,
    plan_artifacts,
)
from .client import (
    BudgetExhaustedError,
    DataQuery,
    DataResult,
    ExperimentBudget,
    KimiIntegrationConfig,
    KimiIntegrationError,
    build_query,
    execute_query,
)
from .contracts import (
    KimiContractError,
    find_api,
    idempotency_key,
    load_contract_snapshot,
    normalize_params,
    virtual_artifact_name,
)
from .transport import (
    CredentialMissing,
    DestinationPolicy,
    GatewayTransport,
    TransportOutcome,
    UrllibTransport,
)

__all__ = [
    "ArtifactLimitError", "ArtifactLimits", "ArtifactPlan", "commit_artifacts",
    "plan_artifacts", "BudgetExhaustedError", "DataQuery", "DataResult",
    "ExperimentBudget", "KimiIntegrationConfig", "KimiIntegrationError",
    "build_query", "execute_query", "KimiContractError", "find_api",
    "idempotency_key", "load_contract_snapshot", "normalize_params",
    "virtual_artifact_name", "CredentialMissing", "DestinationPolicy",
    "GatewayTransport", "TransportOutcome", "UrllibTransport",
]
