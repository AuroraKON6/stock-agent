# R23 接续验收：观测、SLO 与错误分层（KONM-96）

任务定义：[任务书 R23](../GLM_REFACTOR_TASKBOOK_20261020.md)。接续 `82f3482` 的 O 层分层状态
（[REFACTOR_R23_20261023.md](REFACTOR_R23_20261023.md)），基线 `dc8a4cffd51663c0b4f1c67b1f5c03d7f3422071`。
全部输入为合成案件；真实供应商、真实外发、R18 真传输、生产告警接收均为 `NOT_RUN`。

## 1. 结论一览

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Trace | 通过 | `build_trace`：case / business intent → task → 2 个 platform run → query → 4 个 attempt（重试、已对账的 unknown、未对账的 unknown、成功）；业务意图 ID 与平台执行 ID 分列 |
| 观测与对账 | 通过 | `build_reconciliation`：队列、租约、五态账本、查询结果、拒收、缺证据逐项对照权威库；回放 / 估算 / 真实用量按证据分列，未知项保持 `unknown` |
| 技术/业务分层 | 通过 | 同一报告内 technical 与 business 并列；机器异常进 technical 候选，业务门禁只在 business 层且为 `info`；ops/queries 的权限（403）与失败降级（503 仅异常类型名）实测 |
| 零副作用与脱敏 | 通过 | 连续 12 次 HTTP 探针/观测读 + 6 次报告构建后 10 项账本计数不变；输出不含合成令牌与正文标记；关闭采样后业务链 4 个事件照常落库，历史审计行保留 |

## 2. 改动文件

- `stock_agent/observability.py`（追加，既有 `build_observability_status` 输出结构不变）：
  `build_trace`、`build_reconciliation`、`build_metrics`、`build_alert_candidates`、
  `emit_observability_log`、`observability_settings`、`redact`、`build_observability_report`。
- `stock_agent/ops_view.py`：`recent_failures[].error` 改为脱敏输出（见 §5 发现 1）。
- `stock_agent/cli.py`：新增只读子命令 `observability`（以 `read_only=True` 开库，库不存在时拒绝、不创建）。
- `scripts/r23_observability_drill.py`、`deploy/testing/r23_observability.compose.yaml`：容器演练驱动与隔离项目。
- `tests/test_refactor_r23_trace.py`：17 项定向用例（与容器演练共用同一冻结合成案例和断言）。

未改 TaskStore、预算、采纳或 worker 状态流；全部为既有权威账本上的只读投影。

## 3. 设计要点

- **Trace 只带标识、状态、时间**：不含 `input_payload` / `output` / `model_call` / 提示词 / 事件 payload；
  错误文本经 `redact` 截断到 120 字符。attempt 按 `round_index` 排序。
- **用量三分列**：`ledger`（预算账本与五态 attempt 计数）、`replay`（回放模式任务数，明确不计真实费用）、
  `estimated` 与 `real_supplier` 一律 `unknown`——仓库没有估算账本，也没有供应商回执，不编造数字。
- **告警只出候选**：`dispatched=false`、`dispatch=NOT_RUN`；`observability.alerts.enabled` 与
  `observability.sampling.enabled` 默认均为关。配置块畸形时降级为关并置 `config_valid=false`，不影响业务。
- **关闭与回退**：采样关闭时 `emit_observability_log` 返回 `None` 且不写任何输出；观测是纯投影，
  不调用即关闭，账本与审计历史不受影响。

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
$C run --rm -T drill python -B /drills/r23_observability_drill.py seed      # exit 0
$C run --rm -T drill python -B /drills/r23_observability_drill.py report    # exit 0，54 项断言
$C run -d --name r23-serve-konm96 serve
$C exec -T serve python -B /drills/r23_observability_drill.py probe        # exit 0
$C run --rm -T drill python -B /drills/r23_observability_drill.py cross http://<serve 容器 IP>:8770 http://<serve 容器 IP>:8769   # exit 0，4×403
$C run --rm -T drill python -B -m stock_agent observability --config data/config/watchlist.r23.json --task-id <seed 输出的 task_id>
$C down --volumes --remove-orphans
```

## 5. 过程中确认的边界

1. **基线 ops 视图会原样输出阻断原因**：`/api/v1/ops` 的 `recent_failures[].error` 截断但不脱敏，合成
   `Bearer …` 标记实测可从该入口读出。本包改为脱敏输出。
2. **`store.invocation_attempts` 同秒内不按轮次排序**：读取顺序是 `started_at, attempt_id`，同一秒创建的
   attempt 顺序随机。trace 内按 `round_index` 重排后呈现；未改存储读取接口。
3. **`reconcile` 后 state 为 `reconciled`**，不再是 `outcome_unknown` + `reconciled_at`，trace 两种形态都识别。
4. **演练用 internal 网络内服务名解析不可用**（`serve` 解析失败），跨容器检查改用容器 IP；拒绝仍来自
   应用自身的 Host 校验（403），不是网络不通。
