# R23 接续验收：观测、SLO 与错误分层（KONM-96）

任务定义：[任务书 R23](../GLM_REFACTOR_TASKBOOK_20261020.md)。接续 `82f3482` 的 O 层分层状态
（[REFACTOR_R23_20261023.md](REFACTOR_R23_20261023.md)），基线 `dc8a4cffd51663c0b4f1c67b1f5c03d7f3422071`。
全部输入为合成案件；真实供应商、真实外发、R18 真传输、生产告警接收均为 `NOT_RUN`。

本文件描述第 3 轮（sol 第二轮校验返工后）的行为。前两轮缺陷及对应改动见 §6。

## 1. 结论一览

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Trace | 通过 | 冻结合成案例经真实入口建立：Dify HTTP（首个运行 + 回放）、R11 文件桩治理查询、真实 worker + 内存模型桩。case / 业务意图 → task → 2 个 platform run → 1 个查询（经 intake 消费绑定关联）→ 2 个 attempt |
| 观测与对账 | 通过 | 队列、租约、五态账本、查询、拒收、缺证据逐项对照权威库，attempt 与事件按全量聚合；回放 / 估算 / 真实用量分列，未知项保持 `unknown` |
| 技术/业务分层 | 通过 | 同一报告内 technical 与 business 并列；机器异常进 technical 候选，业务门禁只在 business 层且为 `info`；ops/queries 权限（403）与失败降级（503 仅异常类型名）实测 |
| 零副作用与脱敏 | 通过 | 连续 12 次 HTTP 探针/观测读 + 多次报告构建后账本计数与查询账本文件不变；视图只带标识、状态、时间和稳定错误码；结构化日志只带允许字段；关闭采样后真实 worker 照常运行，审计行保留 |

## 2. 改动文件

- `stock_agent/observability.py`（追加；`build_observability_status` 的键不变，`technical.scope`、
  `label_note` 为新增键）：`build_trace`、`build_reconciliation`、`build_metrics`、
  `build_alert_candidates`、`emit_observability_log`、`project_log_fields`、`observability_settings`、
  `read_query_ledgers`、`queries_for_task`、`aggregate_attempts`、`event_type_counts`、`error_code`、
  `safe_label`、`build_observability_report`。
- `stock_agent/research_worker.py`：`run_research_task` 成为薄封装，正常入口结束时上送一条结构化记录；
  原函数体改名 `_run_research_task`，逻辑未动。
- `stock_agent/ops_view.py`：`recent_failures[].error` 由错误原文改为稳定错误码。
- `stock_agent/cli.py`：只读子命令 `observability`（`read_only=True` 开库，库不存在时拒绝、不创建）。
- `scripts/r23_observability_drill.py`、`deploy/testing/r23_observability.compose.yaml`：容器演练。
- `tests/test_refactor_r23_trace.py`：44 项定向用例，与容器演练共用同一冻结合成案例。
- `tests/test_ops_view.py`：`recent_failures` 排序用例改为断言错误码（见 §6 第 2 项）。

未改 TaskStore、预算、采纳或 worker 状态流。`aggregate_attempts` / `event_type_counts` 通过
`store._conn` 做只读 `GROUP BY`（没有新增存储接口）；无 SQLite 连接的存储走逐任务回读并显式标
`scope.complete=false`、列入 `unknown_items`。

## 3. 设计要点

- **视图不带错误原文。** trace、状态里的 `blocked_sample`、ops 的 `recent_failures` 一律输出
  `error_code`（`budget_exhausted` / `evidence_quality` / `invocation_outcome_unknown` /
  `governance` / `lease_expired` / `cancelled` / `timeout` / `auth_rejected` / `rate_limited` /
  `connection_error` / `provider_rejected` / `model_output_invalid` / `other`）。原文留在任务库。
- **结构化日志双重把关。** 字段名必须在允许集合内，值还必须是有界标识符形态或数值；其余字段只计数
  （`dropped_fields`），不记名。因此 `prompt` / `access_token` 这类字段进不来，正文明细放在允许的
  字段名下也会因为不是标识符形态被丢弃。
- **采样接在正常入口。** `run_research_task` 是 CLI run、`worker-service`、`task_intake`、角色执行共用的
  入口；每次运行结束（终态、阻断、异常）上送恰好一条 `research_task_finished`。采样关闭时在读取
  任务状态之前就返回，不产生任何输出。
- **查询关联取自权威记录。** 治理查询把「意图 → 消费该查询的任务」写进 connector intake 的
  `query_consumptions`；trace 以它为准（`link=consumption_binding`），再用 JSONL 审计账本补结果。
  intake 之前、JSONL 里直接带 `task_id` 的旧记录仍可追溯（`link=ledger_task_id`）。
- **账本可用性三分。** `missing` / `unreadable` / `readable_empty` / `readable`，报告与 trace 用同一次
  读取结果。只有后两者算「可读」；查询账本不可读时 `data_query_ledger` 进 `unknown_items`、结果计数
  为空，intake 不可读时 `query_consumption_binding` 进 `unknown_items`。读取 intake 用 `mode=ro`，
  文件不存在时如实报告、不创建。
- **不抽样。** attempt 五态、供应商计数、待对账数和 outbox 事件类型计数都是全量聚合。
- **一份报告一次账本读数。** 整份报告在一个 SQLite 只读快照里构建（`BEGIN` … `ROLLBACK`，不写任何东西），
  attempt 只聚合一次并由状态与各节共用。写入方在报告中途提交，不会让同一报告里的两节对不上。
- **外部关联值只以安全标签出现。** 平台 run ID、provider / 模型名、来源 ID 这类值由外部输入，
  Dify 入口接受一句话当 run ID，provider 名也可以长得像密钥。凡不是合规标识符或带凭据形态的值，
  一律输出 `sha256:` 加 16 位十六进制的稳定摘要；明细（trace 的 attempt）与聚合（`by_provider`）
  用同一个标签函数，计数可对账。
- **查询账本解析失败不等于空账本。** JSONL 非空但没有一条能解析 → `unreadable`；部分行坏 →
  `partially_unreadable`。两者都算不可用：告警观测值为 `unknown`、结果计数为空、
  `data_query_ledger` 进 `unknown_items`。只有真正零行的文件才是 `readable_empty`。
- **读 intake 不创建文件。** 没有 pending WAL 的 intake 以 `immutable` 只读打开（只读挂载同样可用，
  不会生成 `-wal` / `-shm`）。有 pending WAL 的 intake 需要普通只读打开才能看到已提交记录；若做不到
  （例如只读挂载上没有共享内存文件），回退到主文件并置 `intake_wal_pending=true`、
  `query_consumption_binding_wal_pending` 进 `unknown_items`。
- **阈值拒收非有限值。** `NaN` / `Infinity` 使告警能力关闭、配置标无效，报告可被严格 JSON 解析。
- **用量三分列。** `ledger`（预算账本与五态 attempt）、`replay`（回放模式任务数）、`estimated` 与
  `real_supplier` 一律 `unknown`——仓库没有估算账本，也没有供应商回执，不编造数字。
- **配置失败关闭。** `observability.sampling` 与 `observability.alerts` 各自严格校验；哪一块畸形，
  对应能力就关闭并置 `config_valid=false`、`invalid` 列出是哪一块。被拒绝的阈值不会落回默认值以外的值。
- **告警只出候选。** `dispatched=false`、`dispatch=NOT_RUN`。查询账本不可读时 `query_outcome_unknown`
  规则的观测值为 `unknown`、证据为 `unknown`，不触发。

## 4. 复现命令

离线（仓库根目录，`TMPDIR` 指向检出外目录）：

```
python3.12 -B scripts/run_isolated_tests.py --pattern "test_refactor_r23*.py"
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r09.py
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r16.py
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r02.py
python3.12 -B scripts/run_isolated_tests.py --pattern test_ops_view.py
```

容器（镜像 `r25-precheck-74f4813:latest` 只提供锁定依赖，候选代码从检出只读挂载；网络 `internal: true`，
无宿主端口，临时卷）。串行时按团队约定先持有 `fcntl.flock` 的 Docker 锁文件。

```
C="docker compose -f deploy/testing/r23_observability.compose.yaml"
$C run --rm -T drill python -B /drills/r23_observability_drill.py seed      # exit 0；真实入口建案，worker 采样开启
$C run --rm -T drill python -B /drills/r23_observability_drill.py report    # exit 0；断言、零副作用、关采样、5001 任务超限、边界检查
$C run -d --name r23-serve-konm96 serve
$C exec -T serve python -B /drills/r23_observability_drill.py probe        # exit 0
$C run --rm -T drill python -B /drills/r23_observability_drill.py cross http://<serve 容器 IP>:8770 http://<serve 容器 IP>:8769   # exit 0，4×403
$C run --rm -T drill python -B -m stock_agent observability --config data/config/watchlist.r23.json --task-id <seed 输出的 task_id>
$C run --rm -T -v <宿主目录含已关闭的 connector.sqlite3>:/ro-intake:ro drill python -B /drills/r23_observability_drill.py rointake /ro-intake   # exit 0；只读挂载可读、不创建文件
$C down --volumes --remove-orphans
```

`seed` 是唯一写演练库的步骤。`report` 里的「关闭采样」检查会真实运行 worker，它在演练库的副本上
执行；边界检查用系统临时目录下各自的独立库。所以 `report` 可以重复运行，结果一致。

容器以 root 运行，目录权限位对它无效，`report` 里的「只读目录」一项会明确标注跳过；只读场景由
`rointake` 用真正的只读挂载验证。

## 5. 边界

1. **ops 视图的 `error` 语义变了。** 它现在装稳定错误码而不是可读消息。运维在工作台上看具体原因要
   走任务详情；这是为了让 ops 入口不带任何可能含受限正文或令牌的文本。属于需要规划师确认的产品取舍。
2. **`store.invocation_attempts` 不保证执行顺序。** 同一秒内创建的 attempt 返回顺序随机；trace 按
   （fencing token, round, 开始时间, attempt id）重排，未改存储读取接口。
3. **对账后的 state 是 `reconciled`**，不是 `outcome_unknown` 加 `reconciled_at`。trace 两种形态都识别。
4. **演练用 internal 网络内服务名解析不可用**，跨容器检查用容器 IP；拒绝来自应用自身的 Host 校验。
5. **错误码是关键词分类。** 没命中任何类别的消息归 `other`；分类词表在 `observability.py` 的
   `_ERROR_CODE_MARKERS`，需要时按真实错误样本补充。

## 6. 第 1 轮缺陷与对应改动（sol 校验）

| # | 缺陷 | 改动 | 新证据 |
| --- | --- | --- | --- |
| 1 | trace 用 JSONL 的 `task_id` 关联查询，实测 0 条 | 改以 intake 消费绑定为权威关联，保留旧记录的 `task_id` 关联；演练改用 Dify HTTP、真实 worker 和 R11 文件桩治理查询建案 | 演练 `trace.queries=1`、`trace.query_link=consumption_binding`；`test_query_link_comes_from_the_intake_consumption_binding`、`test_legacy_ledger_records_still_link_by_task_id` |
| 2 | 正文和令牌穿过观测输出；采样没接入口 | 视图只输出稳定错误码；日志字段名允许集合 + 标识符形态值；`run_research_task` 结束上送一条记录 | 演练 `trace.no_text_fields`、`log.kept_fields`、`log.body_under_allowed_name`、seed 的 worker 采样开启检查；`NoTextTests`、`StructuredLogTests` |
| 3 | 查询账本缺失被报告为可读 | `read_query_ledgers` 区分四种状态，报告与 trace 共用一次读取 | 演练 `ledger.missing` / `unreadable` / `readable_empty` / `readable`；`LedgerAvailabilityTests` |
| 4 | 超过 5000 任务后 attempt 漏计 | 改为全量 `GROUP BY` 聚合，回退路径显式标范围 | 演练 `scale`（5001 任务，1 个 attempt，五态合计 1）；`test_attempt_beyond_the_old_sample_cap_is_counted` |
| 5 | 畸形配置仍显示告警启用 | 两个能力各自严格校验，失败关闭 | 演练 `config.malformed`、`config.alerts_only_malformed`；`ConfigTests` |

### 第 2 轮（sol 第二轮校验，`d1aa255`）

| # | 缺陷 | 改动 | 新证据 |
| --- | --- | --- | --- |
| 1 | 一句话填进 run ID 被原样输出；像密钥的 provider 名在 `by_provider` 里原样作键 | `safe_label` + 全视图标签化；明细与聚合同一标签 | 演练 `boundary.label.*`；`BoundaryTests`（含 Dify HTTP 入口一例） |
| 2 | 写入方在报告中途提交，五态总计 0、供应商总计 1 | 整份报告一个只读快照；attempt 只聚合一次 | 演练 `boundary.commit.*`；`test_commit_during_a_report_does_not_mix_aggregate_reads` |
| 3 | 坏 JSONL 被当成空账本；读已关闭的 WAL intake 留下 `-wal` / `-shm` | 解析失败与空账本分开，不完整计量为未知；无 pending WAL 用 `immutable` 打开，有则普通只读打开 | 演练 `boundary.ledger.*`、`boundary.intake.*`、容器 `rointake`；`BoundaryTests` |
| 4 | `NaN` 阈值通过校验 | 拒收非有限值 | 演练 `boundary.config.nan` / `.inf`；`test_non_finite_thresholds_fail_closed_and_keep_strict_json` |
