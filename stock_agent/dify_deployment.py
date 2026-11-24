"""Offline Dify deployment inventory and workflow artifact validation.

Read-only groundwork for the Dify adoption plan (GLM_DIFY_ADOPTION_PLAN
section DF-01): a validated, secret-free deployment CANDIDATE (nothing is
installed or started), the phase-one capability requirements to check
against the pinned version's plan features, the Python/Dify authority
boundary, and an inventory mapping the plan's minimal interface contract
onto what ALREADY exists (board HTTP endpoints and CLI) with honest gaps.
R16 adds validation of the digest-bound, replay-only workflow candidate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Pinned 2026-10-13 against upstream evidence (GitHub releases + official docs,
# see docs/implementation/DIFY_DF01_20261013.md §复核与修正): 1.17.1 is the
# latest stable and the FIRST pinned line that satisfies the phase-one
# capability floor — Human Input node needs >=1.13.0, its API >=1.14.1, and
# Human Input inside Iteration/Loop >=1.17.0. The earlier 1.7.2 pin predates
# Human Input entirely and failed the DF-01 acceptance item.
DEPLOYMENT_CANDIDATE_VERSION = "dify-1.17.1-candidate-1"
CANDIDATE_SCHEMA_VERSION = "1.0"

_FORBIDDEN_KEY_MARKERS = ("api_key", "apikey", "password", "secret", "token")
_R16_RELEASE = "1.17.1"
_R16_RELEASE_COMMIT = "8387590ace4a094de812b7847fc6a4c3a27cd52b"
_R16_ORIGIN = "http://stock-agent-r16-gateway:8770/api/dify/v1/"
_R16_ENVIRONMENT = {
    "STOCK_AGENT_SERVICE_SECRET": ("secret", ""),
    "STOCK_AGENT_WORKFLOW_VERSION": ("string", ""),
    "R16_CREATE_UNCONFIRMED": ("string", "create_unconfirmed"),
    "R16_STATUS_UNCONFIRMED": ("string", "status_unconfirmed"),
    "R16_CASE_PATH_DEFAULT": ("string", "/"),
}
_R16_AUTH = {
    "type": "api-key",
    "config": {"type": "bearer", "api_key": "{{#env.STOCK_AGENT_SERVICE_SECRET#}}"},
}


class DeploymentCandidateError(ValueError):
    pass


def _validate_replay_graph(graph: dict) -> None:
    nodes = graph["nodes"]
    by_id = {node["id"]: node["data"] for node in nodes}
    if len(nodes) != len(by_id) or {key: node["type"] for key, node in by_id.items()} != {
        "16001": "start", "16003": "http-request", "16005": "if-else",
        "16006": "http-request", "16007": "if-else", "16008": "end",
        "16009": "end", "16010": "end",
    }:
        raise DeploymentCandidateError("workflow must use only its reviewed native nodes")
    edges = [(edge["source"], edge["sourceHandle"], edge["target"]) for edge in graph["edges"]]
    if sorted(edges) != [
        ("16001", "source", "16003"), ("16003", "source", "16005"),
        ("16005", "false", "16009"), ("16005", "true", "16006"),
        ("16006", "source", "16007"), ("16007", "false", "16010"), ("16007", "true", "16008"),
    ]:
        raise DeploymentCandidateError("workflow must create once and query only after a receipt")
    for node_id, operation, fields in (
        ("16003", "create-research", {
            "title": "{{#16001.title#}}",
            "case_id": "{{#16001.case_id#}}",
            "research_question": "{{#16001.research_question#}}",
            "input_version": "{{#16001.input_version#}}",
            "business_intent_id": "{{#16001.business_intent_id#}}",
            "dify_run_id": "{{#sys.workflow_run_id#}}",
            "workflow_version": "{{#env.STOCK_AGENT_WORKFLOW_VERSION#}}",
            "execution_mode": "replay",
        }),
        ("16006", "research-status", {
            "task_id": "{{#16003.headers.r16_task_id#}}",
            "status_only": "true",
        }),
    ):
        call = by_id[node_id]
        if call.get("authorization") != _R16_AUTH or call.get("headers") != "" or call.get("params") != "":
            raise DeploymentCandidateError("HTTP node must use the runtime bearer secret and no custom headers or query")
        if (call.get("url") != _R16_ORIGIN + operation or call.get("method") != "POST"
                or call.get("retry_config") != {"retry_enabled": False, "max_retries": 0, "retry_interval": 0}
                or call.get("timeout") != {"connect": 2, "read": 3, "write": 2}):
            raise DeploymentCandidateError("HTTP destination, method, retry or timeout exceeds the replay policy")
        if call.get("body") != {"type": "x-www-form-urlencoded", "data": [
            {"type": "text", "key": key, "value": value} for key, value in fields.items()
        ]}:
            raise DeploymentCandidateError("HTTP node must send the reviewed replay form")
        if call.get("error_strategy") != "default-value" or call.get("default_value") != [
            {"key": "body", "type": "string", "value": "{}"},
            {"key": "status_code", "type": "number", "value": 0},
            {"key": "headers", "type": "object", "value": "{}"},
        ]:
            raise DeploymentCandidateError("HTTP errors must route to an unconfirmed receipt")

    for branch, reply, header in (("16005", "16003", "r16_task_id"), ("16007", "16006", "r16_status")):
        cases = by_id[branch]["cases"]
        if len(cases) != 1 or cases[0]["case_id"] != "true" or cases[0]["logical_operator"] != "and":
            raise DeploymentCandidateError("workflow must gate the confirmed receipt branch")
        conditions = [(item["variable_selector"], item["comparison_operator"], item["value"])
                      for item in cases[0]["conditions"]]
        if conditions != [
            ([reply, "status_code"], "=", "200"),
            ([reply, "body"], "contains", '{"ok": true, "data": {"task_id": "task_'),
            ([reply, "headers", header], "not empty", ""),
        ]:
            raise DeploymentCandidateError("HTTP status alone cannot confirm a replay receipt")

    for node_id, status, task_id, path in (
        ("16008", ["16006", "headers", "r16_status"],
         ["16003", "headers", "r16_task_id"], ["16003", "headers", "r16_case_path"]),
        ("16009", ["env", "R16_CREATE_UNCONFIRMED"], [], ["env", "R16_CASE_PATH_DEFAULT"]),
        ("16010", ["env", "R16_STATUS_UNCONFIRMED"],
         ["16003", "headers", "r16_task_id"], ["16003", "headers", "r16_case_path"]),
    ):
        outputs = [(item["variable"], item["value_selector"]) for item in by_id[node_id]["outputs"]]
        if outputs != [
            ("status", status), ("task_id", task_id), ("case_path", path),
            ("case_id", ["16001", "case_id"]), ("business_intent_id", ["16001", "business_intent_id"]),
        ]:
            raise DeploymentCandidateError("workflow outputs must use the reviewed receipt projection")


def validate_replay_workflow(manifest_path: Path, *, approved_version: str | None = None) -> str:
    """Return the digest-based version of the R16 candidate after offline validation."""
    from .release_controls import build_secret_scan

    try:
        import yaml
    except ImportError as exc:
        raise DeploymentCandidateError("workflow validation requires the locked PyYAML dependency") from exc

    try:
        manifest = json.loads(manifest_path.read_bytes())
        if (manifest["schema_version"] != 1 or manifest["dify_release"] != _R16_RELEASE
                or manifest["dify_release_commit"] != _R16_RELEASE_COMMIT
                or manifest["execution_mode"] != "replay"
                or manifest["artifact"] != "single-research-replay.yml"):
            raise DeploymentCandidateError("unexpected replay workflow manifest")
        artifact = manifest_path.with_name(manifest["artifact"])
        raw = artifact.read_bytes()
        if len(raw) > 64 * 1024:
            raise DeploymentCandidateError("workflow DSL exceeds the size limit")
        version = "sha256:" + hashlib.sha256(raw).hexdigest()
        if version != manifest["workflow_version"] or (approved_version is not None and version != approved_version):
            raise DeploymentCandidateError("workflow digest does not match the manifest or reviewed version")
        # The reviewed file is a self-contained export. Aliases can obscure which
        # values the importer actually uses.
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(raw)):
            raise DeploymentCandidateError("workflow DSL must not use YAML aliases or anchors")
        document = yaml.safe_load(raw)
        if (document["kind"] != "app" or document["version"] != "0.3.1"
                or document["app"]["mode"] != "workflow"
                or document.get("dependencies") != []):
            raise DeploymentCandidateError("workflow must be a standalone app with no plugin dependencies")
        workflow = document["workflow"]
        variables = workflow["environment_variables"]
        if (len(variables) != len(_R16_ENVIRONMENT)
                or {item["name"]: (item["value_type"], item["value"]) for item in variables} != _R16_ENVIRONMENT):
            raise DeploymentCandidateError("workflow environment must use empty runtime values and fixed fallbacks")
        if any(isinstance(feature, dict) and feature.get("enabled") for feature in workflow["features"].values()):
            raise DeploymentCandidateError("workflow features must be disabled for replay")
        _validate_replay_graph(workflow["graph"])
    except DeploymentCandidateError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError):
        raise DeploymentCandidateError("invalid replay workflow manifest or DSL") from None
    if build_secret_scan([artifact], [], datetime.now(timezone.utc))["status"] != "PASS":
        raise DeploymentCandidateError("workflow DSL did not pass the release secret scan")
    return version


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
            "form": "docker-compose (Dify official) inside WSL2 Ubuntu（Docker Engine 原生安装，非 Docker Desktop）",
            "bind": "loopback-only reverse proxy; no public exposure",
            "prerequisites_missing": [
                "Docker Engine 未安装（Windows 宿主与 WSL2 Ubuntu 内均无 docker CLI；安装需负责人授权）",
            ],
            "environment_measured": {
                "measured_at": "2026-10-13（只读探测，未启动任何服务）",
                "wsl": "WSL2 Ubuntu 在位（Stopped），发行版内 docker 未安装",
                "wsl_disk": "VHD 所在卷 1007G，可用 954G",
                "wsl_memory_limit": "7.6Gi（满足 Dify 最低 4G，需与既有业务进程共存评估）",
                "windows_c_free": "29G（89% 已用）→ 部署不落 C 盘",
                "windows_e_free": "151G",
                "candidate_ports_free": "80/443/3000/5001/8080/5432/6379 当前无监听",
                "reserved_ports": "8767/8768 为本项目面板保留，Dify 不得占用",
            },
            "compose_profile": {
                "note": "候选记录，不安装：1.17.1 官方 compose 含 api/api_websocket/worker/"
                        "worker_beat(dify-api:1.17.1)、web(dify-web:1.17.1)、postgres:15-alpine、"
                        "redis:6-alpine、sandbox(dify-sandbox:0.2.15)、local_sandbox、"
                        "plugin_daemon(dify-plugin-daemon:0.6.10-local)、agent_backend、"
                        "ssrf_proxy 与 agent_ssrf_proxy(ubuntu/squid)、certbot、nginx、weaviate:1.39.2",
                "host_ports": "仅 EXPOSE_NGINX_PORT(80)/SSL(443)/PLUGIN_DEBUGGING(5003) 需回环绑定；"
                              "容器网内 5001/5432/6379/3128/8194 不出宿主",
                "floating_tag_risk": "官方 compose 对 nginx/squid/certbot/postgres/redis/busybox 用浮动 tag，"
                                     "违反本项目固定基线规则；DF-03 安装时改为具体版本+镜像摘要（当前无 docker 无法解析，登记待办）",
            },
            "ports": {"host_plan": "仅 127.0.0.1 绑定（EXPOSE_NGINX_PORT 绑 127.0.0.1），具体端口 DF-03 部署前定"},
        },
        "capability_facts": {
            "verified_at": "2026-10-13 在线核实（GitHub Releases + docs.dify.ai + ssrf 模板源码）；DF-03 临时实例实测前不声称可用",
            "human_input_node": "1.13.0 引入；1.14.1 增加 Human Input API；1.17.0 支持嵌入 Iteration/Loop → 1.17.1 全部满足",
            "human_input_limits": "邮件链接任何持有人可回复（无需账号，先到先得）；Web app 表单不适用于 Trigger 启动的流程；"
                                  "超时默认 3 天须显式连接超时分支 → 受保护动作必须走后端真实用户主体验证，Human Input 仅作展示引导层",
            "workflow_dsl_export": "自托管长期内置（1.17.1 具备）",
            "service_api": "workflow app 级 API key（1.17.1 具备）",
            "ssrf_protection": "ssrf_proxy(Squid) 默认拒绝私网 + 兜底 deny all；经 include 片段（dify_allow_private.conf）"
                               "精确放行后端 host:port，不关闭全局防护",
            "license": "Dify Open Source License = Apache 2.0 + 两条附加条件（禁多租户 SaaS、须保留品牌）；"
                       "本项目单租户自托管内部用途不触犯条件1；按项目规则登记 REVIEW_REQUIRED，不代替负责人批准",
        },
        "capabilities_required": [
            {"capability": "workflow_api_trigger", "why": "业务入口经 API 触发流程（第一阶段 1）",
             "verify_how": "DF-03 用临时实例实测；未验证前不部署"},
            {"capability": "human_input_node", "why": "等待补证/人工问题节点（第一阶段 6）",
             "verify_how": "DF-03 临时实例实测节点行为",
             "available_since": "1.13.0（API 1.14.1；Iteration/Loop 内嵌 1.17.0）——1.17.1 锚定满足"},
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
            "（已核实 2026-10-13）版本能力线：Human Input 1.13.0+/API 1.14.1+/循环内嵌 1.17.0+，1.17.1 满足；详见 capability_facts 与 DF-01 文档 §复核与修正",
            "（已核实 2026-10-13）许可：Dify OSL 单租户自托管允许，登记 REVIEW_REQUIRED 待负责人",
            "（已核实 2026-10-13）磁盘/内存/端口：见 deployment.environment_measured",
            "部署实例的数据备份与留存上限策略（Dify 侧运行日志/文件同样纳入数据权限，部署前由负责人确认）",
            "镜像摘要固定：需 docker 环境解析 digest（DF-03 安装时完成）",
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
               "capability_facts", "python_boundary", "open_questions", "blocked_items"}
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
