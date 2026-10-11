# G0 可信发布基线操作手册

## 1. 目标与边界

G0 只证明一个候选版本可识别、可复现、可审阅和可回滚。它不批准数据供应商，不批准外部 LLM
外发，不批准生产档案，不安装计划任务，也不形成业务 Go/No-Go。技术 `READY` 与业务启用必须分别报告。

禁止通过删除失败项、伪造 Git 提交、代填法务意见或降低测试门槛来获得 `READY`。

## 2. 固定输入

- 源码和受控配置：由 Git 候选提交标识。
- Python 依赖：`requirements.lock.txt`，包含精确版本和分发文件 SHA-256。
- Pi 运行时：`pi-agent-core` 固定版本、`package-lock.json`、隔离生产设置和方法供应链 lock。
- 业务投影：仅做哈希和新鲜度观察，不转化为技术或业务批准。
- 模型配置：仅登记 provider、模型名、脱敏 endpoint 和密钥是否存在，不保存密钥值。

## 3. 首次建立隔离环境

需要已安装 `uv`。创建环境会安装锁中依赖，属于工程准备动作，不属于日常审计：

```powershell
uv venv --python 3.11 .venv311
uv pip sync --python .venv311\Scripts\python.exe requirements.lock.txt
uv venv --python 3.12 .venv312
uv pip sync --python .venv312\Scripts\python.exe requirements.lock.txt
```

依赖发生变更时，先人工审阅 `requirements-optional.txt`，再显式重生成 lock。不得在日常运行中自动
升级版本，也不得用当前环境的临时状态替代 lock。

## 4. 标准执行顺序

### 4.1 供应链控制

默认离线运行：

```powershell
python -m stock_agent release-controls --config config/watchlist.local.json
```

仅在需要刷新 Python 精确版本许可证元数据时，显式允许访问官方 PyPI：

```powershell
python -m stock_agent release-controls --config config/watchlist.local.json --fetch-python-metadata
```

产物：

- `reports/release/secret_scan_latest.json`
- `reports/release/sbom.cdx.json`
- `reports/release/license_report_latest.json`
- `reports/release/python_registry_metadata.json`
- `reports/release/latest.json`
- `reports/release/latest.md`

Secret scan 只保存路径、行号、模式、值长度和值哈希，不保存疑似密钥或上下文。CycloneDX SBOM 同时覆盖
Python lock、npm lock 和经审查的 Pi 方法。未知、copyleft、复合或拒绝许可证必须进入人工复核，Agent
不得自动批准。

### 4.2 双版本 Python 证明

```powershell
python -m stock_agent python-env-audit --run-tests --require-pass
```

默认验证 `.venv311` 和 `.venv312`。只有两个版本均与同一 lock 精确匹配、关键包冒烟导入通过、完整测试
通过时才报告 `PASS`。报告不保存原始测试日志，只保存退出码、测试数、跳过数、时长和输出 SHA-256：

- `reports/release/python_environment_latest.json`
- `reports/release/python_environment_latest.md`

### 4.3 Canonical manifest

```powershell
python -m stock_agent release-audit --config config/watchlist.local.json
python -m stock_agent release-audit --config config/watchlist.local.json --require-ready
```

普通模式始终写报告，便于读取阻断原因。严格模式只有技术状态为 `READY` 才返回 0；`BLOCKED` 返回 2。
manifest ID 来自稳定输入投影，不包含生成时间和密钥存在布尔值。

## 5. G0 验收矩阵

| 门禁 | 自动证据 | 合格条件 | 人工责任 |
|---|---|---|---|
| 源码身份 | Git HEAD、工作树状态 | 有有效 HEAD 且候选干净 | 用户批准首个源码基线 |
| 凭据卫生 | 脱敏 secret scan | 真实密钥发现数为 0 | 轮换任何已暴露密钥 |
| 依赖复现 | 哈希 lock、双版本审计 | 3.11/3.12 均匹配并通过测试 | 审阅依赖变更 |
| 供应链清单 | CycloneDX SBOM | 结构有效且输入无解析错误 | 审阅新增组件 |
| 许可证 | license report | `PASS` | 法务/合规裁决 review 项 |
| Pi 隔离 | 固定版本、settings、skill lock | 全部一致且安全 | 审阅方法或版本变更 |
| Manifest | release audit | 严格命令退出 0 | 发布责任人签认候选 |

## 6. 当前状态（2026-09-06 夜间刷新）

- Python lock：31 个精确版本组件，带分发文件哈希（`uv pip compile`，目标 3.11/windows）。
- Python 3.11.15：锁一致，810 项测试通过，跳过 2 项（`python-env-audit` 单版本 PASS）。
- Python 3.12.13：锁一致，810 项测试通过，跳过 2 项；双版本联合审计 **PASS**（3.12 由 `uv python install 3.12` 用户级安装）。
- Python 3.8.5（历史基座）：810 项测试通过，保持可回退。
- Secret scan：`PASS`，真实密钥阻断发现为 0。
- CycloneDX SBOM：`PASS`，182 个组件。
- License report：`REVIEW_REQUIRED`，`certifi` 与 `tqdm`（MPL-2.0 表达式）待真实法务/合规复核；Agent 不得代批。
- 备份与恢复演练：`backup-create`/`backup-verify`/`restore-drill` 全 PASS（259 文件，RTO 6.9s，live 源未覆盖）；本机技术 PASS，生产 DR 保持 BLOCKED（外部依赖）。
- RSS 采集安全加固：解析前拒绝序言含 DOCTYPE 的 feed（实体注入防御），新增 8 个测试；三环境全量 810 通过。
- Git：暂存集 281 文件已对账（`reports/handoff/baseline_include_20260906T031635.list/.sha256`），但首个提交被仓库级 mimosa git-gate（pre-commit L3 深扫）拦截；26 个 high 经逐条分诊全部为误报或工具产物（报告：`reports/handoff/mimosa_triage_20260906.md`；`.mimosa/security-policy.json` 已含 threatModel 豁免但 git-gate 不消费）。等用户决断（--no-verify 放行 / 要求进一步修复 / 保留阻塞）。
- Canonical release audit：`BLOCKED`（4 项 blocker：Git HEAD 缺失、工作树不干净、license 待裁决、生产灾备未 READY）；业务启用仍为 `NOT_ASSERTED`。

因此当前不能宣称 G0 完成，更不能宣称生产或业务上线。下一批人工动作是：对首提交阻塞做出决断、轮换曾在聊天中暴露的
DeepSeek key、完成许可证判断；完成后重新生成全部控制证据并运行严格审计。

## 7. 停止条件

发现真实密钥、锁漂移、任一 Python 版本失败、SBOM 解析错误、许可证未决、Pi 生产设置不安全、无有效
Git HEAD 或工作树不干净时，立即停止发布候选推进并保持 `BLOCKED`。不得自动提交、自动批准或自动上线。
