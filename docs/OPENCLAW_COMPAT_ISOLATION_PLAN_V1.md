# OpenClaw 历史兼容层隔离计划 v1

> 日期：2026-09-17（Asia/Shanghai）
> 计划依据：主计划 v31 风险 #12（"OpenClaw 历史兼容仍进入部分控制代码"）、战略 v30 §18.2；
> G0 交付清单"OpenClaw 兼容层隔离计划"
> 状态：**PLAN**（本文件只定计划；执行在前述风险关闭后分步进行，每步全量测试）

## 1. 现状盘点（2026-09-17 全量核对）

| 依赖面 | 位置 | 与生产的关系 | 风险 |
|---|---|---|---|
| `stock_agent/openclaw_handoff.py`（228 行） | 主包 | **被生产使用**：`agent_handoff.py` L48 复用 `build_openclaw_handoff` / `validate_openclaw_handoff` 构造与校验 public handoff | 命名误导：生产 Pi 路径实际运行"OpenClaw 命名"的代码 |
| `doctor.py` `_check_openclaw` / `_check_openclaw_handoff`（L174-232） | 检查器 | 已不被 `run_doctor` 调用（L228-232 注释保留 fail-closed 遗留语义），但 markdown 渲染器仍保留 openclaw 类别标签 | 死代码 + 状态解释歧义 |
| `research_cases.py` / `research_git.py` / `research_runtime.py` 的事件转发 | 主包 | 转发目标是 `brokerage_architecture`（项目自有事件台账），**不是** OpenClaw；但采用"临时替换模块级 project_path"的旧模式（与 `openclaw_handoff` 同期代码） | 并发不安全；曾被误认为 OpenClaw 耦合 |
| `scripts/retire_openclaw_stock_agent.ps1`、`update_openclaw_stock_agent_models.ps1` | 脚本 | 只操作 OpenClaw 注册表条目，一次性迁移工具 | 与现行脚本混放 |
| `openclaw/`（29 文件）、`.openclaw/`、`openclaw-workspace-state.json` | 磁盘 | 历史只读/回滚材料；`openclaw/` 已 gitignore（9-06 基线决定） | 无（仅磁盘存在） |
| `config` 无 OpenClaw 段；`pi_agent.disable_legacy_http_fallback` 已关 | 配置 | 生产配置无 OpenClaw 入口 | 无 |

**结论**：生产主路径唯一真实依赖是 `agent_handoff.py` 对 `openclaw_handoff.py` 的函数复用；
其余为死代码、历史脚本与磁盘档案。

## 2. 隔离目标

1. 主流程（`pipeline`、`agent_handoff`、`doctor`）不再 import 名为 `openclaw_*` 的模块。
2. OpenClaw 命名的代码集中于 `stock_agent/compat/` 包，明示"只读兼容，不得新增能力"。
3. 三处 monkey-patch 事件转发迁移到 `config.project_path_override`（并发安全）。
4. 对外行为零变化：92 个 CLI 命令、报告、治理结论不变；全量测试每步通过。

## 3. 分步执行方案

### Step 1：抽取共享 handoff 核心（无行为变化）
- 新建 `stock_agent/handoff_core.py`：把 `openclaw_handoff.py` 中与 OpenClaw 无关的通用逻辑
  （public 信号五道闸筛选 `_handoff_allowed`、最小化构造 `_public_signal`、payload 自校验哈希、
  `validate` 骨架、安全文本扫描）原样迁移。
- `openclaw_handoff.py` 变薄壳：`from .handoff_core import ...` + re-export（保持 `agent_handoff`
  的 import 不变，本步零调用方修改）。
- 测试：现有 `test_openclaw_handoff_v24.py`、`test_agent_handoff_v25` 全过 + 新增 handoff_core 直测。

### Step 2：生产路径改用核心模块
- `agent_handoff.py` 改为 `from .handoff_core import ...`。
- `openclaw_handoff.py` 移入 `stock_agent/compat/openclaw_handoff.py`，顶层保留弃用别名
  `stock_agent/openclaw_handoff.py`（一行 re-export + DeprecationWarning，保留一个弃用期）。
- `doctor.py` 两个遗留检查函数移至 `compat/`，渲染器类别标签改 `legacy_compat`。
- 测试：全量 + doctor 相关测试适配。

### Step 3：monkey-patch 迁移（并发安全修复）
- `research_cases.py` L659-669、`research_git.py` L504-512、`research_runtime.py` L1338-1352 的
  "临时替换 `brokerage_architecture.project_path`"改为 `project_path_override(...)` 上下文
  （`research_runtime` 内部已有该模式的先例 L94-104）。
- 测试：三模块现有测试 + 新增并发冒烟（两线程同时转发事件不互相踩踏）。
- **注意**：这三个文件在 mimosa 视角含既有 finding（如 `research_cases.py:404` SSRF 误报），
  编辑存在自锁风险；执行前先按 `reports/handoff/mimosa_triage_20260906.md` 的等价重写经验准备。

### Step 4：脚本与磁盘档案归档
- `scripts/retire_openclaw_stock_agent.ps1`、`update_openclaw_stock_agent_models.ps1` 移入
  `scripts/legacy/`（README 标注一次性迁移工具、默认 DryRun）。
- `openclaw/`、`.openclaw/` 磁盘目录在首次 Git 基线后可选移至仓库外存档（同 9-06 `skills` 先例），
  保留 README 指针。

## 4. 验收与回滚

- 每步验收：`python -m unittest discover -s tests` 全绿（当前基线 834）+ `doctor` 无新 WARN +
  `agent-handoff --check` 哈希链验证通过。
- 每步一个 Git 提交（回滚单位）；Step 2/3 之后跑一次 `release-controls` 刷新 SBOM 对源码指纹。
- 回滚：按步 revert 即可，各步无跨步耦合。

## 5. 明确不做

- 不删除 `openclaw/` 磁盘历史（审计与回滚证据，交接书 §5.3）。
- 不恢复任何 OpenClaw 调度能力；`runtime=pi` 是唯一生产运行时。
- 不在本计划内顺手改其他技术债（如 `action_history` 截断、`bot` 前缀误伤——另列 backlog）。
