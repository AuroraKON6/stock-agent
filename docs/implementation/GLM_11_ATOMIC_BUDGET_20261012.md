# GLM-11 交付报告（2026-10-12，第二批）：原子预算预留 + SSE 慢消费者实测

执行：GLM。范围：计划 GLM-11 明列的"预算跨并发任务原子预留"与故障矩阵的"SSE 慢消费者"两项。

## 缺陷三（本批抓到）：预算检查-记账竞态

**缺陷**：GLM-05B 的预算门禁是"检查（读 outbox 计数）→ 记账（写 attempt 事件）"两步——两个并发 worker（各自连接）可同时通过 limit-1 的检查、各自记账并各自调用模型，**超出预算一次**。计划原文明确要求原子预留。

**修复**：`TaskStore.reserve_model_attempt` —— 当日 attempt 事件计数与新事件插入在**同一个 BEGIN IMMEDIATE 事务**内完成；跨连接串行化保证并发下恰一人拿到最后一个名额。预留事件即审计（调用前落库，结果未知也可追溯）；**拒绝不记账**（被拒检查不消耗预算）。worker 侧：`_invoke_model` 内先预留，被拒抛 `_BudgetExhausted` → 任务 blocked（gate=budget 带当时计数）；既有只读视图继续用 `may_start_model_call`。

**实测**：barrier 同步的两个并发预留（各自连接）在 limit=1 下恰一真一假；并发 live worker（各自 store）**模型入口总调用数恰为 1**，一 succeeded 一 blocked。

## SSE 慢消费者（矩阵项实测）

`create_task_board_server` 新增可调 `sse_max_seconds`/`sse_keepalive_seconds`（生产默认不变）；两项真实 loopback 测试：**不读的消费者**——有界流在限时内自行结束（客户端读到 EOF），服务器线程池随后仍健康服务请求；**正常读的消费者**——在限时内收到积压事件（task_created）。此前有界流只是实现声明，现在有实测证据。

## 状态

- 原子预算预留（store/budget_guard/worker）：**IMPLEMENTED + VERIFIED（离线并发实测）**
- SSE 慢消费者有界流：**IMPLEMENTED + VERIFIED（真实 loopback）**
- 磁盘满/网络中断/费用未知外呼注入：**NOT_RUN（后续；多需受控注入环境）**

## 改动文件

- `stock_agent/task_runtime/store.py` — `reserve_model_attempt`（原子计数+插入；拒绝零写入）
- `stock_agent/budget_guard.py` — `reserve_model_attempt` 包装（读配置限额）
- `stock_agent/research_worker.py` — 移除循环内两步门禁；`_invoke_model` 内原子预留；`_BudgetExhausted` → blocked(gate=budget)；成功轮记录预算快照
- `stock_agent/task_board.py` — SSE 限时/keepalive 服务器参数化（默认不变）
- 新增测试 `tests/test_budget_reservation.py`（4 用例）、`tests/test_sse_slow_consumer.py`（2 用例）

## 正负用例（先失败后实现）

并发预留恰一真一假（barrier+各自连接）；拒绝不产生 attempt 事件；限额 0（默认）全放行；并发 live worker 模型调用总数恰 1、状态 [blocked, succeeded]；不读消费者限时内 EOF+服务器健康；读消费者收到积压。

开发期修正（如实）：测试助手多做 mark_running 致 worker 转换非法（两 worker 崩于预算门前——以"模型调用数 0"暴露）；一处传参笔误；一处残句即刻清除。

## 实际命令与结果

- 专项 4+2 全 OK；邻近 test_budget_guard / test_fault_matrix / test_research_worker_rounds / test_daily_task_cycle / test_overview / test_ops_view 全 OK（旧预算测试在新原子路径下不变绿不收）。
- 全量隔离回归：**Ran 1193 tests, errors=14（既有原生守卫）, skipped=2, failures=0**（以 %TEMP%\glm11b_regression_20261012.log 为准）。
- 浏览器 UI/原生/live/真人：未执行；零模型调用。

## 生产配置、权限、账本变化

无（行为变化仅"并发下不再超支"——缺陷修复方向）。

## 回滚

还原四文件增量、删测试。预算回滚后恢复两步检查（即回到缺陷态）——不建议。

## 遗留与下一批

- 矩阵剩余：磁盘满/网络中断/费用未知外呼注入（多需受控环境）；原生 VM 全项。
- 待用户清单不变：ADR-002、来源批准、vault 事故处置、mimosa 完整审计。
