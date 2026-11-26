# macOS 本机运行手册（stock-agent）

本文说明如何在一台干净的 macOS（Apple Silicon，arm64）上从零检出、安装、启动并验证
stock-agent。Windows 入口（`scripts/*.ps1`、计划任务）保持不变；本文只覆盖 macOS / Linux，
不改变任何业务语义、登记账本或权限边界。

验证环境：macOS 27.0.1（arm64）、uv 0.13.5、CPython 3.12.15、Node 24.22.1、
Docker 29.9.2（aarch64）。

## 1. 前置版本

| 组件 | 版本 | 用途 | 缺失时的后果 |
| --- | --- | --- | --- |
| `uv` | 0.13.x | 创建锁定的 `.venv312`、安装带哈希的依赖 | 入口脚本拒绝启动（`Locked interpreter not found`） |
| CPython 3.12 | 3.12.x（`uv python install 3.12` 可自动获取） | 项目锁定解释器 | 同上；PATH 上的 `python` 不是受支持的回退 |
| Xcode 命令行工具 | 任一近期版本 | `ps`、编译部分 wheel | 部分依赖安装失败 |
| Node.js + npm | 22+（验证于 24.22.1） | `pi_runtime` 固定版本 Pi Agent | `pi-runtime-status` 报 BLOCKED；离线主流程不需要 |
| Docker Desktop（Apple Silicon） | 29.x | 镜像构建与容器 worker | 仅 Docker 相关步骤不可用 |

`python3`（系统自带 3.9 即可）只用于运行 `scripts/local_ops.py`；业务代码始终由
`.venv312` 解释器执行。

## 2. 干净检出与安装

```bash
git clone git@github.com:AuroraKON6/stock-agent.git
cd stock-agent

# 2.1 锁定解释器 + 依赖（带哈希校验；版本与 Windows 锁 requirements.lock.txt 一致）
uv venv .venv312 --python 3.12
uv pip install --python .venv312/bin/python --require-hashes \
  -r requirements.macos-arm64-py312.lock.txt

# 2.2 Pi Agent 运行时（按 package-lock.json 固定版本安装，不执行安装脚本）
(cd pi_runtime && npm ci --ignore-scripts --omit=dev --no-audit --no-fund)

# 2.3 本机凭据（可选；示例配置离线即可跑通）
cp .env.example .env.local      # 然后只在本机填写；.env.local 已被 .gitignore 忽略
```

关于依赖锁：

- `requirements.lock.txt` 是 **Windows 主机锁**（Python 3.11、`--python-platform windows`），
  在 macOS 上无法安装；`requirements.linux-py312.lock.txt` 只用于容器镜像。
- `requirements.macos-arm64-py312.lock.txt` 是本手册新增的 macOS arm64 主机锁，
  版本与 Windows 锁逐项一致（仅 `colorama`、`tzdata` 两个 Windows 专用项不同）。
- 发布审计（`release-controls` / `python-env-audit`）仍以 `requirements.lock.txt` 为准，
  本文件不是发布身份输入。

**检出位置要求**：把仓库放在真实路径下（例如 `~/src/stock-agent`）。不要放在 `/tmp`、
`/var`、`/etc` 之下 —— macOS 把它们链接到 `/private/*`，而证据库与运行清单的
“路径不得经过符号链接 / 重解析点”检查会拒绝任何位于别名路径下的文件。这是有意的安全
边界，不是缺陷。

## 3. 启动与停止

所有入口都是 `scripts/local_ops.py` 的薄包装；Windows 上同一个包装会把参数转交给对应的
`.ps1`，所以 Windows 仍只有一套实现。

| 动作 | macOS / Linux | Windows（不变） |
| --- | --- | --- |
| 单次研究运行 | `scripts/run_once.sh` | `scripts\run_once.ps1` |
| 前台持续运行 | `scripts/watch.sh --interval-minutes 30` | `scripts\watch.ps1` |
| 后台守护进程 启/停 | `scripts/start_agent.sh` / `scripts/stop_agent.sh` | `scripts\start_agent.ps1` / `stop_agent.ps1` |
| 研究工作台 启/停 | `scripts/start_research_workbench.sh` / `scripts/stop_research_workbench.sh` | `scripts\start_research_workbench.ps1` / `stop_research_workbench.ps1` |
| Go/No-Go 只读检查 | `scripts/go_live_check.sh` | `scripts\go_live_check.ps1` |
| 构建镜像 | `scripts/build_image.sh` | 同左（bash） |

工作台参数与 PowerShell 版一致：`--config`、`--host`（仅回环地址）、`--port`（默认 8767）、
`--no-browser`、`--state-directory`（默认 `logs/workbench`）。PID 与元数据在
`logs/workbench/workbench.{pid,json}`，stdout/stderr 为同目录的时间戳日志。

**进程身份规则**（与 PowerShell 版相同）：只在记录的 PID 命令行仍然包含本项目的入口、
配置、host、port，或 `/health` 返回的 `instance_id` + `pid` 与记录一致时才动作；PID 被复用
或已失效时只清理元数据，绝不终止无关进程。停止优先走带回环实例头的 `/shutdown` 端点，
其次 `SIGTERM`，最后才 `SIGKILL`。

### 3.1 安全空跑（不联网、不调用模型）

`config/watchlist.example.json` 只启用本地 JSONL 情报池，所有行情源与 LLM 均关闭：

```bash
scripts/run_once.sh
```

预期输出（最后一行起）：

```text
完成：新增 4 条，预警 3 条。
日报: <仓库>/reports/daily/<时间戳>_0800.md
结构化结果: <仓库>/reports/json/<时间戳>_0800.json
预警: <仓库>/reports/alerts/<时间戳>_0800.jsonl
```

退出码 `0`。首次运行会先打印
`WARNING: config/watchlist.local.json not found; using development example config.`
—— 没有 `config/watchlist.local.json` 时入口总是明确声明使用的是开发示例配置。

### 3.2 研究工作台

```bash
scripts/start_research_workbench.sh --no-browser     # 本机打开浏览器时去掉 --no-browser
curl -s http://127.0.0.1:8767/health                  # {"status": "ok", "instance_id": "...", "pid": ...}
scripts/stop_research_workbench.sh
```

`start` 幂等：同配置/端口的存活实例直接复用；不同配置/端口的存活实例只报告、不覆盖。

## 4. 验证

### 4.1 全量离线测试

```bash
.venv312/bin/python scripts/run_isolated_tests.py
```

运行器会禁止测试写入真实检出、禁止外部网络和子进程。它在 macOS 上会自动把临时目录换到
物理路径（`/tmp/...` → `/private/tmp/...`），否则证据库相关测试会因系统别名被拒。

本次迁移在干净检出上的结果：**1674 个测试，3 失败 / 17 错误 / 9 跳过，退出码 1**。
失败项全部是“既有失败”，都与 macOS 无关，而是依赖 Windows 开发机上的未跟踪状态：

| 现象 | 数量 | 依赖的本机状态 |
| --- | --- | --- |
| `FileNotFoundError: config/watchlist.local.json`（含 3 个模块导入失败） | 13 | 本地配置文件（gitignored） |
| `openclaw/stock_agent_project/registry_export.json` 不存在 | 2 | 已退役的旧目录（gitignored） |
| 测试写真实检出的 `data/review` 被隔离器拦截 | 2 | 检出内已有运行数据目录 |
| `llm_success 0 != 2` | 1 | 环境中有 `DEEPSEEK_API_KEY`（设任意占位值即通过） |
| `socket_permission_denied not found` | 1 | 同上 |
| `'stock-agent' != 'stock agent'` | 1 | 测试硬编码检出目录名必须是 `stock agent` |

对照基线（同一台机器、未改动的 `main`）：不装依赖、临时目录为别名路径时为
1636 个测试，12 失败 / 134 错误；装上依赖并改用物理临时目录后为 1639 个测试，
3 失败 / 21 错误。迁移改动新增 35 个测试（`tests/test_local_ops.py`、
`tests/test_isolated_test_guard.py`），全部通过，未新引入失败。

### 4.2 其它只读检查

```bash
.venv312/bin/python -m stock_agent doctor --config config/watchlist.example.json
.venv312/bin/python -m stock_agent pi-runtime-status --config config/watchlist.example.json
```

`doctor` 在 macOS 上的预期结果：`FAIL: 0`，并有一条平台相关的 WARN
`Cannot check scheduled tasks (PowerShell unavailable)`（计划任务检查只实现了 Windows）。
`pi-runtime-status` 在未配置 API Key 时为 `BLOCKED`、`isolated settings: PASS`，属预期。

## 5. Docker（Apple Silicon）

```bash
# 配置校验（STOCK_AGENT_TAG 为必填，缺失时 compose 拒绝渲染浮动标签）
export STOCK_AGENT_TAG='<tag>@sha256:<digest>'    # 取自 deploy/docker/image_binding.json
docker compose -f deploy/compose.yaml config
(cd deploy/dify && docker compose --env-file compose.example.env config)

# 受控构建（会写入 deploy/docker/image_binding.json，要求工作树干净或 --allow-dirty）
scripts/build_image.sh
```

基础镜像以多架构索引摘要固定，`python:3.12-slim-bookworm` 与 `node:22-slim` 在 arm64 上
可用；`requirements.linux-py312.lock.txt` 的 wheel 在 aarch64 上齐全。本次已在本机
arm64 原生构建镜像，并以与 `deploy/compose.yaml` 等价的约束（`--read-only`、
`--cap-drop ALL`、`no-new-privileges`、命名卷、tmpfs、无网络）启动，健康检查命令与
离线 `run` 均成功，产物正确落在 `APP_*` 卷中。

Compose 文件本身不含 Windows 专有路径（全部为命名卷），无需 macOS 覆盖文件。
`deploy/testing/` 下的演练覆盖需要各自的 `R25_*` 变量，属 R25 演练工具，不是日常入口。

## 6. 配置与密钥注入

| 内容 | 位置 | 是否入库 |
| --- | --- | --- |
| 主配置 | `config/watchlist.example.json` → 复制为 `config/watchlist.local.json` | 模板入库，`.local.json` 不入库 |
| 凭据模板 | `.env.example`（只有占位符） | 入库 |
| 凭据实体 | `.env.local`（`.gitignore` 的 `.env.*` 忽略） | 不入库 |
| Dify 回放环境 | `deploy/dify/compose.example.env` → 本地 env 文件 + `--env-file` | 模板入库 |

注入方式：

1. **未跟踪本地文件（项目约定）**：`.env.local`。Python CLI 启动时由
   `stock_agent.config.load_local_secrets()` 读入环境变量，且**文件值优先于 shell 继承值**。
2. **环境变量**：`DEEPSEEK_API_KEY` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` /
   `XUEQIU_TOKEN` / `ALLTICK_TOKEN` / `FREE_QUOTE_TOKEN` / `PI_AGENT_BINARY`。
3. **钥匙串（可选）**：`security find-generic-password -w -s <service> -a <account>`
   在启动 shell 中导出；仓库不保存钥匙串引用。

注意：不经 CLI 入口的调用（例如裸 `python -m unittest`）不会读取 `.env.local`，
只认真实环境变量。

## 7. Windows 专有项盘点

| 类别 | 项 | 状态 |
| --- | --- | --- |
| 启动/停止脚本 | `run_once` / `watch` / `start_agent` / `stop_agent` / `start_research_workbench` / `stop_research_workbench` / `go_live_check` | **已移植**（`scripts/local_ops.py` + 同名 `.sh`；`.ps1` 保留为 Windows 入口） |
| 构建脚本 | `build_image.sh`、`r25_image_drill.sh` | 本来就是 bash；补了可执行位（原为 644） |
| 依赖锁 | `requirements.lock.txt` 仅 Windows | **已补** `requirements.macos-arm64-py312.lock.txt` |
| 凭据模板 | 无 `.env.example` | **已补** |
| 计划任务 | `install/uninstall/set_scheduled_tasks_enabled.ps1`、`run_daily_research.ps1` | **未移植**（见下） |
| 桌面快捷方式 | `create_dashboard_shortcut.ps1`、`create_research_workbench_shortcuts.ps1` | **不适用**：macOS 直接 `open reports/dashboard/latest.html` 或访问工作台 URL |
| 旧运行时运维 | `migrate_to_pi_agent.ps1`、`rollback_pi_agent.ps1`、`retire_openclaw_stock_agent.ps1`、`update_openclaw_stock_agent_models.ps1` | **未移植**：属 Windows 一次性迁移/回滚脚本 |
| 本地身份 | `stock_agent.local_identity`（`windows_local`，读取 Windows 令牌 SID） | **按设计在 macOS 不可用**：摘要复核/查看器的本地人工身份校验会拒绝；未改动 |
| doctor 计划任务检查 | `doctor._check_scheduled_tasks`（PowerShell） | macOS 上为 WARN，未改动 |
| 路径/编码 | 代码内未发现盘符或反斜杠硬编码；`__main__` 仅在 win32 重设控制台编码，macOS 默认 UTF-8 | 无需改动 |
| 行尾 | 仓库有 30 个 CRLF、3 个混合行尾的已跟踪文件（含 `cli.py`、`pyproject.toml`） | Python/JSON 解析不受影响；未做全仓规范化，避免无关 diff |
| 文件系统大小写 | 未发现依赖大小写不敏感的路径拼接 | 无需改动 |

### 7.1 定时调度在 macOS 上怎么办（待决策）

`run_daily_research.ps1` 是带调度所有权语义的编排脚本（`scheduled-task-submit --scheduler
legacy-powershell`、`cutover-legacy-check`），与 R25 的调度切换门禁绑定，直接改写会改变
运行契约，因此本次**没有**移植。macOS 上的可选方向，需要 HORIZON K 决定：

- **A. 容器 worker（推荐）**：按 R25 设计由容器内的调度 worker
  （`python -m stock_agent worker-service`，镜像由 `scripts/build_image.sh` 构建，
  运行契约见 `deploy/compose.yaml`）承担定时，主机不装计划任务。
- **B. launchd + 主机编排移植**：新增 `run_daily_research` 的跨平台实现和
  `~/Library/LaunchAgents` plist，调度所有权需登记为新的 scheduler id。

## 8. 运行数据（不自动迁移）

本次迁移**没有**复制 Windows 上的任何数据库、台账或证据卷。主流程在没有历史数据的
情况下即可运行（去重与历史信号库会从空库开始）。如需延续历史，最小数据集如下，
是否迁移、何时迁移由 HORIZON K 决定：

| 数据 | 位置（相对仓库根） | 格式 | 用途 |
| --- | --- | --- | --- |
| 本地配置 | `config/watchlist.local.json`、`config/*.local.json` | JSON | 主题、来源、规则 |
| 凭据 | `.env.local` | KEY=VALUE | 重新在本机录入，不要复制明文文件 |
| 信号与任务库 | `data/stock_agent.sqlite3`（及 `-wal`/`-shm`） | SQLite | 去重、历史信号、任务队列 |
| 人工情报池 | `data/inbox/*.jsonl` | JSONL（UTF-8） | 输入 |
| 复核与决策 | `data/review/`、`data/decisions/` | JSON/JSONL | 人工把关记录 |
| 证据库 | `data/evidence_vault/` | 内容寻址对象 + 哈希链 | 官方原件与摘录 |
| 调度所有权登记 | `data/scheduler_ownership/` | JSON | 调度切换门禁 |

迁移步骤（获批准后执行）：在 Windows 上停掉所有入口 → 用 `python -m stock_agent
backup-create` 生成带哈希链的备份包 → 只复制备份包到 macOS → 在 macOS 上
`backup-verify` 校验哈希链 → `restore-drill` 演练通过后再恢复到正式目录。不要直接
拷贝正在使用的 `.sqlite3`（WAL 不一致），也不要绕过备份包的密钥扫描与 JSONL 完整性检查。

## 9. 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Locked interpreter not found: .../.venv312/bin/python` | 未创建 `.venv312` | 执行 §2.1 |
| `reparse point in evidence object path` / `crosses a symlink or reparse point` | 仓库或临时目录位于 `/tmp`、`/var`、`/etc` 等别名路径下 | 把仓库移到真实路径；跑测试用 `scripts/run_isolated_tests.py` |
| 测试报 `workspace write denied: .../data/review` | 检出里已有运行数据 | 用干净检出跑测试，或接受这些既有失败 |
| 一半测试报 `No module named 'yaml'` | 未安装依赖锁 | 执行 §2.1 |
| `pi-runtime-status` 为 BLOCKED | 未 `npm ci` 或未配置 API Key | 执行 §2.2 / §6 |
| `docker compose` 报 `STOCK_AGENT_TAG ... set` | 未固定镜像摘要 | 按 `deploy/docker/image_binding.json` 导出 `STOCK_AGENT_TAG` |
| `scripts/xxx.sh: Permission denied` | 文件无可执行位 | `bash scripts/xxx.sh` 或 `chmod +x scripts/*.sh` |
| 工作台 `Refusing to start: Host ... is not a loopback address` | 绑定非回环地址 | 只用 `127.0.0.1`、`localhost` 或 `::1` |
