# [30-05] pi_runtime.py — Pi 隔离子进程运行时（v25.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 761 | pi_skills、audit_log、model_operations | llm_agent、role_analyst、agent_handoff、doctor、cli |

## 职责
把 Pi coding-agent 当作**唯一的受控 LLM 执行 harness**：无工具、无会话、无动态插件，从 stdin 收治理批准的 prompt，输出严格校验，审计 fail-closed。工作流控制、门禁、人类决策全部留在 Python。

## 关键构件（call_pi_agent L109-274）
1. 隔离硬编码 — `allow_tools/allow_sessions/allow_packages/allow_extensions/allow_project_trust` 全 False（L95-99），**不受配置影响**。
2. 版本钉死 — `PINNED_PI_VERSION="0.84.2"`（L32），不符即 PiRuntimeError（L127-130）。
3. 命令解析 `resolve_pi_command()`（L277）— `PI_AGENT_BINARY` → `pi_runtime/node_modules/.../cli.js` + node → PATH。
4. 供应链强制 — `compile_role_system_prompt()`，不 READY 即阻断（L149-151）。
5. 提示词落盘 `_materialize_system_prompt()`（L659）— `data/pi_runtime/system_prompts/<role>_<sha256[:16]>.md`。
6. CLI 旗标（L158-178）— `--mode json --print --no-session --no-tools --no-approve --no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files`。
7. 环境隔离 `_isolated_environment()`（L644）— `SAFE_ENV_KEYS` 白名单（L39-60），HOME/USERPROFILE 重定向到 role 专属 workspace，`PI_CODING_AGENT_DIR` 指向隔离 agent_home。
8. 输出校验 — stdout 4MB/stderr 256KB 上限；`tool_call_count != 0` → "emitted a tool call despite --no-tools"；stop_reason 必须 stop；assistant 文本非空。
9. **审计 fail-closed（L221-265）** — finally 中写哈希链到 `logs/pi_runtime/calls_YYYYMMDD.jsonl`（input/output/system_prompt 哈希 + skill bundle 三哈希 + tools_enabled=[] + project_trust="denied"）；**审计写入失败且调用成功时抛错丢弃输出**（L262-264）。
10. `system_fingerprint = "pi:<ver>:<audit_event_id>"`（L272）— 与审计记录的关联键。

## 状态与 UAT
- `pi_runtime_status()`（L351）— 隔离 settings 逐键校验（defaultProjectTrust=never、defaultTools=[] 等）+ lock 哈希 + 供应链 READY → 写 `reports/pi_agent/runtime/latest.*`。
- `run_pi_runtime_uat/run_pi_skills_uat`（L467/L535）— 合成公共输入的实弹验收，输出须与固定 JSON contract **逐键全等**。

## 备注 / 风险
`SAFE_ENV_KEYS` 有 `"HTPPS_PROXY"` 拼写冗余项（无害，提示白名单靠人工维护）；错误文本经 `_sanitize_error()`（L736）redact 密钥与 Windows 路径后才入审计。
