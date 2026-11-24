from __future__ import annotations

import contextvars
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# R25: a deployment relocates the project trees through the APP_* roots of
# runtime_layout (compose mounts them as volumes). The evidence tree is
# listed first because it sits INSIDE the data tree on the host and has its
# own root in a deployment: the longest matching prefix wins. On the host
# none of the variables is set, so every relative path still resolves under
# PROJECT_ROOT exactly as before.
RELOCATABLE_PROJECT_TREES = (
    ("data/evidence_vault", "APP_EVIDENCE_ROOT"),
    ("config", "APP_CONFIG_ROOT"),
    ("data", "APP_STATE_ROOT"),
    ("reports", "APP_REPORT_ROOT"),
)
_relocated_roots_cache: list = [(), {}]


def relocated_project_roots() -> Dict[Tuple[str, ...], Path]:
    """Project trees whose APP_* root is set in the environment, keyed by
    their relative path parts (longest first)."""
    key = tuple(os.environ.get(variable, "") for _, variable in RELOCATABLE_PROJECT_TREES)
    cached_key, cached_roots = _relocated_roots_cache
    if key != cached_key:
        cached_roots = {}
        for (tree, _variable), value in zip(RELOCATABLE_PROJECT_TREES, key):
            if value.strip():
                cached_roots[Path(tree).parts] = (PROJECT_ROOT / value.strip()).resolve()
        _relocated_roots_cache[:] = [key, cached_roots]
    return dict(cached_roots)


# Central project-path override. Modules that captured ``project_path`` at
# import time still honor an active override because the shared function
# consults this context variable on every call. contextvars scopes the override
# to the current thread / asyncio task, so it never leaks across concurrency
# domains and is always restored when its context manager exits.
_PROJECT_PATH_OVERRIDE: contextvars.ContextVar[Optional[Callable[[Any], Any]]] = contextvars.ContextVar(
    "stock_agent_project_path_override", default=None
)


def project_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    override = _PROJECT_PATH_OVERRIDE.get()
    if override is not None:
        return Path(override(path_value))
    for parts, root in relocated_project_roots().items():
        if path.parts[:len(parts)] == parts:
            rest = path.parts[len(parts):]
            return root.joinpath(*rest) if rest else root
    return PROJECT_ROOT / path


def project_workspace_roots() -> List[Tuple[Tuple[str, ...], Path]]:
    """Every place ``project_path`` can resolve into, as ``(project-relative
    prefix, directory)``: the relocated trees first (a nested tree such as
    the evidence vault must win over ``data``), then the project root. With an
    active override only that root exists."""
    override = _PROJECT_PATH_OVERRIDE.get()
    if override is not None:
        return [((), Path(override(".")).resolve())]
    return [*(relocated_project_roots().items()), ((), PROJECT_ROOT)]


def project_relative_path(path_value: Union[str, Path]) -> str:
    """The project-relative spelling of ``path``: what ``project_path`` takes
    to reach the same file again. Files under a relocated tree keep their
    ``data/...``-style name so bundles and manifests stay layout-neutral;
    anything outside the project roots is returned as an absolute string."""
    resolved = Path(path_value).resolve()
    for prefix, root in project_workspace_roots():
        if _is_below(resolved, root):
            return Path(*prefix, resolved.relative_to(root)).as_posix()
    return str(resolved)


def within_project_roots(path_value: Union[str, Path]) -> bool:
    """True when ``path`` sits below one of the roots ``project_path`` uses."""
    resolved = Path(path_value).resolve()
    return any(_is_below(resolved, root) for _prefix, root in project_workspace_roots())


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@contextmanager
def project_path_override(resolver: Optional[Callable[[Any], Any]]) -> Iterator[None]:
    """Temporarily reroute every ``project_path`` resolution in this context.

    ``resolver`` maps a relative path string to a ``Path`` (usually anchored at
    an isolated test root). Passing ``None`` or the normal ``project_path``
    function is a no-op, so production behavior is unchanged and recursion is
    avoided. Absolute paths are never rerouted (``project_path`` returns them
    before consulting the override). State is always restored, even on
    exception, and the override cannot leak across threads or asyncio tasks.
    """
    if resolver is None or resolver is project_path:
        yield
        return
    token = _PROJECT_PATH_OVERRIDE.set(resolver)
    try:
        yield
    finally:
        _PROJECT_PATH_OVERRIDE.reset(token)


def load_local_secrets() -> Dict[str, str]:
    """Load the approved local credential store into the environment.

    ``.env.local`` (gitignored) is the operator-approved credential store for
    this single-user workstation. The FILE takes precedence over inherited
    environment variables: placing a key in the store is the operator's most
    recent deliberate credential decision and must not be silently shadowed
    by a stale shell-level value (a stale inherited key produced exactly that
    bug on 2026-09-21). No credential literals live in source, examples, or
    tests.
    """

    path = project_path(".env.local")
    if not path.is_file():
        return {}
    loaded: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        os.environ[key] = value
        loaded[key] = "<set>"
    return loaded


def load_config(path_value: str) -> Dict[str, Any]:
    path = project_path(path_value)
    if not path.exists():
        raise FileNotFoundError("Config not found: {0}".format(path))

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in [".yaml", ".yml"]:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError("YAML config requires PyYAML. Install requirements-optional.txt.") from exc
        return yaml.safe_load(text)
    return json.loads(text)


def ensure_directories() -> None:
    for relative in [
        "data",
        "data/inbox",
        "data/inbox/filings",
        "data/review",
        "data/review/queue_actions",
        "data/decisions",
        "data/events",
        "data/events/compact",
        "data/remediation",
        "data/research_cases",
        "data/research_runtime",
        "data/research_workpapers",
        "data/evidence_vault",
        "data/evidence_vault/objects/sha256",
        "data/evidence_vault/quarantine/sha256",
        "data/business_onboarding",
        "data/research_operations",
        "data/research_git",
        "data/llm_governance",
        "data/llm_governance/audit",
        "data/llm_governance/exceptions",
        "data/pi_runtime",
        "data/pi_runtime/system_prompts",
        "data/pi_runtime/workspace",
        "data/model_operations",
        "data/model_operations/usage",
        "data/production_pilot",
        "data/value_validation",
        "data/value_validation/sealed",
        "data/backup_restore",
        "data/backup_restore/bundles",
        "data/backup_restore/drills",
        "data/run_coordination",
        "data/run_coordination/leases",
        "data/run_coordination/recovered",
        "data/run_coordination/manifests",
        "data/workspace/inbox",
        "data/cache",
        "data/cache/market_quotes",
        "reports",
        "reports/daily",
        "reports/json",
        "reports/alerts",
        "reports/actions",
        "reports/architecture",
        "reports/team",
        "reports/decisions",
        "reports/review",
        "reports/review_queue",
        "reports/market_data_governance",
        "reports/cases",
        "reports/research_runtime",
        "reports/research_workpapers",
        "reports/evidence_vault",
        "reports/evidence_vault/uat",
        "reports/business_onboarding",
        "reports/business_onboarding/intake_pack",
        "reports/research_operations",
        "reports/research_git",
        "reports/llm_governance",
        "reports/model_operations",
        "reports/handoff",
        "reports/agent_handoff",
        "reports/pi_agent",
        "reports/pi_agent/public_handoff",
        "reports/pi_agent/runtime",
        "reports/pi_agent/skills",
        "reports/pi_agent/skills_uat",
        "reports/production_pilot",
        "reports/value_validation",
        "reports/value_validation/blind_assignments",
        "reports/workspace",
        "reports/source_health",
        "reports/remediation",
        "reports/dashboard",
        "reports/workbench",
        "reports/notifications",
        "reports/health",
        "reports/release",
        "reports/release_audit",
        "reports/backup_restore",
        "reports/run_coordination",
        "logs/scheduled",
        "logs/go_live",
        "logs/run_ops",
        "logs/run_coordination",
        "logs/production_pilot",
        "logs/pi_runtime",
    ]:
        project_path(relative).mkdir(parents=True, exist_ok=True)
