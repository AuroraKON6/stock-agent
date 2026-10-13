"""DF-01: Dify deployment candidate, capability matrix, interface inventory.

Read-only groundwork for the Dify adoption plan (GLM_DIFY_ADOPTION_PLAN
section DF-01): a validated, secret-free deployment CANDIDATE (nothing is
installed or started), the phase-one capability requirements to check
against the pinned version's plan features, the Python/Dify authority
boundary, and an inventory mapping the plan's minimal interface contract
onto what ALREADY exists (board HTTP endpoints and CLI) with honest gaps.
No service, no network, no credentials.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

DEPLOYMENT_CANDIDATE_VERSION = "dify-1.7.2-candidate-1"
CANDIDATE_SCHEMA_VERSION = "1.0"

_FORBIDDEN_KEY_MARKERS = ("api_key", "apikey", "password", "secret", "token")


class DeploymentCandidateError(ValueError):
    pass


def deployment_candidate() -> Dict[str, Any]:
    """The pinned, secret-free deployment candidate for DF-01 review.

    Dify ships as docker-compose; this machine has WSL2/Ubuntu but no
    Docker installed yet - the candidate records that prerequisite gap
    instead of presuming an installation.
    """
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "dify_version": DEPLOYMENT_CANDIDATE_VERSION,
        "deployment": {
            "form": "docker-compose (Dify official) inside WSL2 Ubuntu",
            "bind": "loopback-only reverse proxy; no public exposure",
            "prerequisites_missing": [
                "Docker Engine 未安装（WSL2 Ubuntu 在位但 docker CLI 缺失）",
                "磁盘/内存配额未测量",
            ],
            "compose_profile": {
                "note": "候选记录，不安装：services=api/worker/web/db/redis/ssl 仅标识",
            },
            "ports": {"host_plan": "仅 127.0.0.1 绑定，具体端口 DF-02 前定"},
        },
        "capabilities_required": [
            {"capability": "workflow_api_trigger", "why": "业务入口经 API 触发流程（第一阶段 1）",
             "verify_how": "DF-03 用临时实例实测；未验证前不部署"},
            {"capability": "human_input_node", "why": "等待补证/人工问题节点（第一阶段 6）",
             "verify_how": "DF-03 临时实例实测节点行为"},
            {"capability": "service_credential_scoping", "why": "受限服务凭据调后端，用途不由模型参数扩大（第一阶段 2）",
             "verify_how": "DF-02/DF-03 验证凭据 scope 与越权拒绝"},
            {"capability": "workflow_version_export", "why": "流程版本/制品哈希可追溯（DF-03 验收）",
             "verify_how": "DF-03 导出 DSL 并登记哈希"},
        ],
        "python_boundary": {
            "python_owns": [
                "TaskStore/案件状态机、预算、幂等、外发治理、证据验真、结果引用校验",
                "案件完成/取消/等待人/签收的权威判定；Dify 节点绿色不构成批准",
            ],
            "dify_must_not": [
                "直连业务数据库（SQLite 只归 Python 进程）",
                "双执行：生产单任务只选一个执行器（过渡期 Pi，DF-04 起逐模块切换）",
                "凭节点凭据绕过预算或外发审查",
                "把生产提示词编辑当免审热更新（须发布候选流程版本并评测）",
            ],
            "id_binding": "case_id/task_id/input_version/attempt_id/dify_run_id/workflow_version",
        },
        "open_questions": [
            "选定 Dify 版本与自托管许可条款逐条核对（DF-01 验收前完成）",
            "Human Input / API 触发在选定版本与版本权益下可用性需实测确认",
            "端口/磁盘/内存配额与备份策略（部署前由负责人确认）",
        ],
        "blocked_items": [
            "安装系统组件/启动服务：BLOCKED（DF-01 只出候选矩阵，不安装）",
            "开放公网：BLOCKED（仅 loopback 候选）",
            "任何真实凭据/密钥：BLOCKED（候选配置零密钥）",
            "生产流程切换/双执行：BLOCKED（DF-04 对照验收后单选）",
        ],
    }


def _reject_secret_keys(node: Any, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _FORBIDDEN_KEY_MARKERS):
                raise DeploymentCandidateError(
                    "candidate must stay secret-free: {0}.{1}".format(path, key))
            _reject_secret_keys(value, "{0}.{1}".format(path, key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_secret_keys(item, "{0}[{1}]".format(path, index))


def validate_deployment_candidate(document: Any) -> None:
    if not isinstance(document, dict):
        raise DeploymentCandidateError("candidate must be a JSON object")
    allowed = {"schema_version", "dify_version", "deployment", "capabilities_required",
               "python_boundary", "open_questions", "blocked_items"}
    unexpected = set(document) - allowed
    if unexpected:
        raise DeploymentCandidateError(
            "unknown candidate fields: {0}".format(sorted(unexpected)))
    if str(document.get("dify_version") or "") != DEPLOYMENT_CANDIDATE_VERSION:
        raise DeploymentCandidateError(
            "dify_version must be the pinned candidate {0!r}".format(DEPLOYMENT_CANDIDATE_VERSION))
    caps = document.get("capabilities_required")
    if not isinstance(caps, list) or not caps:
        raise DeploymentCandidateError("capabilities_required must be a non-empty list")
    for cap in caps:
        if not isinstance(cap, dict) or not str(cap.get("capability") or "").strip():
            raise DeploymentCandidateError("each capability needs a name")
    _reject_secret_keys(document)


def load_deployment_candidate(config: Dict[str, Any]) -> Dict[str, Any]:
    from .config import project_path

    raw = (config or {}).get("dify") or {}
    path_value = str(raw.get("config_path") or "config/dify.deployment.json")
    path = project_path(path_value)
    if not path.exists():
        return {"configured": False, "status": ["blocked: 未部署（DF-01 仅候选）"]}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentCandidateError("candidate unreadable: {0}".format(exc)) from exc
    validate_deployment_candidate(document)
    return {"configured": True, "candidate": document, "path": path_value}


def interface_inventory() -> Dict[str, Any]:
    """Map the Dify plan's minimal interface contract onto what exists today.

    Only REAL, already-tested entry points are listed with exists=true;
    everything phase one still needs is an honest gap. Dify calls Python,
    never the database directly.
    """
    interfaces: List[Dict[str, Any]] = [
        {"contract_role": "create_research", "exists": True,
         "surface": "CLI task-create --from-symbols (intake 服务)",
         "notes": "幂等已验（问题/事件身份键）；HTTP 化为 gap"},
        {"contract_role": "research_status", "exists": True,
         "surface": "GET /api/v1/tasks 与 /api/v1/tasks/{id}（面板）",
         "notes": "长任务查询有界重试由 Dify 侧执行（第一阶段 4）"},
        {"contract_role": "human_feedback", "exists": True,
         "surface": "POST /api/v1/human-responses + CLI human-response",
         "notes": "actor 身份机制待 ADR-002 验收后扩展页面写路径"},
        {"contract_role": "evidence_snapshot", "exists": True,
         "surface": "GET /api/v1/overview、/api/v1/events-view、/api/v1/queries-view",
         "notes": "获准上下文快照的机器可读投影已就绪"},
        {"contract_role": "model_governance", "exists": True,
         "surface": "llm_governance + budget_guard + research_worker 逐轮治理",
         "notes": "DF-04 受控模型边界在此之上做 Dify 等价机制"},
    ]
    gaps = [
        {"gap": "认证的 HTTP 业务入口", "why": "Dify 需要 HTTP 调用后端；现有写入口是 CLI + 单人 CSRF 面板路由",
         "plan_ref": "DF-02 受控 API"},
        {"gap": "服务凭据 scope 与轮换", "why": "Dify 以受限服务凭据调用；签发/范围/撤销机制未建",
         "plan_ref": "DF-02"},
        {"gap": "dify_run_id/workflow_version 绑定字段", "why": "任务契约尚未携带 Dify 运行关联（DF-02 加）",
         "plan_ref": "DF-02"},
    ]
    return {"schema_version": "1.0", "interfaces": interfaces, "gaps": gaps}
