# [80-02] cli.py — 命令行接口（约 90 个子命令）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 80-orchestration | 3054 | 全部模块 | 用户、scripts/*.ps1、AGENTS.md 文档流程 |

## 职责
`main()` 单入口 argparse 定义 + 大型分发全部子命令；`_policy_call()` 统一包装写操作（ValueError/TimeoutError → "Policy denied" + exit 2）。

## 命令分组（v31 后）
| 组 | 命令 |
|---|---|
| 基础运行 | run / agent-run / watch / init-config / list-skills |
| 复核决策 | export-review / review-summary / review-queue-sync / review-queue-status / review-queue-action / decision-list / decision-record / decision-summary / decision-confirm / decision-reject / decision-defer |
| 补救 | attach-evidence-url / attach-structured-fields / resolve-reject-defer-remediation / review-priority |
| 案件运行时 | case-list / case-record / case-summary / research-runtime-sync / research-runtime-status / research-runtime-action |
| 证据库 | evidence-vault-import / -fetch(--live) / -uat(--live) / -link / -status / -archive-review / -activate / -revoke |
| 底稿 | research-workpaper-sync / -status / -verify |
| 运营 | research-ops-check / research-ops-plan |
| Research-as-Git | research-stage/-commit/-approve/-reject/-defer/-status/-auto-stage |
| 行情工作区 | workspace-sync / market-cache-seed / market-data-governance-status |
| 呈现 | dashboard / review-workbench(--once) |
| 事件台账 | architecture-sync / event-sync / event-state / event-log / event-compact / event-health / event-audit |
| 健康 | alerts / doctor |
| 治理模型 | llm-governance-status / llm-governance-review / model-ops-status |
| Pi | pi-runtime-status / pi-runtime-uat(--live) / pi-skills-status / pi-skills-lock(--write) / pi-skills-uat(--live) |
| Handoff | agent-handoff(--check) / pi-handoff-run / openclaw-handoff |
| 试点业务 | production-pilot-sync/-status(--require-go)/-approval/-activate/-revoke/-incident/-incident-action / business-onboarding-pack/-sync/-status |
| 控制证据供应商 | control-evidence-register/-review/-revoke/-status / provider-init/-due-diligence/-review/-activate/-suspend |
| **v31/v32 新增** | **release-controls(--fetch-python-metadata/--require-pass) / python-env-audit(--run-tests) / release-audit(--require-ready) / backup-create / backup-verify / restore-drill / backup-restore-status / value-validation-status + 11 个 value-*（study-init/case-register/case-exclude/cohort-freeze/baseline-record/treatment-freeze/assisted-record/blind-prepare/blind-evaluate/blind-reveal/gate-decision，全部强制 --actor/--actor-role）** |

## 设计纪律
- fail-closed 标志：联网/重建 lock/实弹测试必须显式 `--live`/`--write`/`--fetch-python-metadata`，否则 SystemExit。
- `--require-*` 门槛命令状态不达标即非零退出——脚本链据此阻断。
- 受保护命令的角色校验在模块层完成（cli 只透传 actor/role）。

## 备注 / 风险
92→90 入口的收敛是战略 §18.2 要求；本文件已是最大模块（3054 行），新增命令前先确认无既有能力（AGENTS.md 明示）。
