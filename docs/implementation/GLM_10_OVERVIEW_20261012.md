# GLM-10 交付报告（2026-10-12，第一片）：工作台总览聚合

执行：GLM。范围：统一面板的数据地基——一个只读总览投影（引擎/行情/事件/任务/预算五段），经纯函数 + board `GET /api/v1/overview` + CLI `overview` 三个入口暴露。遵循 ADR-001 第一阶段（扩展现有 task_board，零新依赖）；本片不含 UI（下一片）。

## 工作包与具体用户流程

一条命令（或一个 HTTP GET）回答"现在整个工作台什么状态"：

- **engine**：事件流归哪个引擎（batch/task；非法配置如实报 invalid 而不是猜）；
- **market**：主题标的清单 + 缓存行情快照（价格/涨跌/来源时间与状态）+ **缺失清单**（缺行情如实列出，不伪造）；
- **events**：24h 回看内研究合格事件数 + 最近 10 条（signal_id/标题/类型/发布时间）+ 选择器报告（example 溯源排除在此生效）；
- **tasks**：任务引擎状态计数、剩余队列、**等待人工的问题清单**（task_id + 问题文本 + 更新时间）、outbox 事件数、消费账本摘要；任务库不存在时 `available: false` 如实报告；
- **budget**：今日模型调用次数 / 上限 / 是否启用。

## 状态

- 总览聚合（纯函数）：**IMPLEMENTED + VERIFIED（离线）**
- board 端点：**IMPLEMENTED + VERIFIED（真实 loopback HTTP）**
- CLI：**IMPLEMENTED + VERIFIED（离线）**
- 总览 UI（页头/面板渲染）：**NOT_RUN（下一片，需扩展浏览器验收）**
- 市场/待办/运行完整页面（GLM-10 表格全量）：**NOT_RUN（后续片）**

## 改动文件

- 新增 `stock_agent/overview.py`（build_overview 纯投影；复用 execution_mode/_quote_snapshots/load_research_signals/budget_guard/consumption_summary，不自建第二套事实）
- 修改 `stock_agent/task_board.py`（GET /api/v1/overview；投影异常 503 如实降级）
- 修改 `stock_agent/cli.py`（`overview` 子命令；任务库缺失时 store=None）
- 新增测试 `tests/test_overview.py`（7 用例）

## 正负用例（先失败后实现）

主题标的去重 + 空缓存缺失清单；引擎两模式 + 非法值；事件计数排除 example 溯源；任务段（succeeded/awaiting_human 计数、等待问题文本、预算 calls_today=1/limit=0/disabled、消费摘要）；store 缺失 available=false；真实 loopback HTTP 五段齐全；CLI JSON 输出含 scope_note。

## 实际命令与结果

- 专项 test_overview 7 OK（一次通过）；邻近 test_task_board_sse / test_task_detail / test_config_defaults OK。
- 全量隔离回归：**Ran 1165 tests, errors=14（既有原生守卫）, skipped=2, failures=0**（以 %TEMP%\glm10_regression_20261012.log 为准）。
- 浏览器 UI/原生/live/真人：未执行；零模型调用。

## 生产配置、权限、账本变化

无（纯只读投影）。

## 回滚

删除 overview.py/测试、还原 board 与 cli 两处增量。无数据迁移。

## 遗留与下一批

- 总览 UI 片：board 页头渲染五段概要 + 浏览器检查扩展（真实 Chromium）。
- GLM-10 后续片：事件视图（去重事件+修订+关联案件）、待办视图（按权限）、运行视图（来源健康/额度/门禁）——依赖来源批准的部分保持 BLOCKED。
- 待用户清单不变：ADR-002、来源批准、vault 事故处置、mimosa 完整审计。
