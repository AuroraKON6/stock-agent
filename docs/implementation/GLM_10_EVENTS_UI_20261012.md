# GLM-10 交付报告（2026-10-12，第四片）：事件区 UI

执行：GLM。范围：任务板新增可折叠"事件"区，消费第三片的 `/api/v1/events-view` 投影。**真实 Chromium 验收 22/22 通过**（18 项既有 + 4 项新增）。

## 工作包与具体用户流程

打开任务板，总览条下方即是事件区（默认展开，可折叠）：每个故事事件一行——最新标题、**修订徽章**（如 "v1/2 修订"）、来源芯片（报道渠道）、标的、报道数（×N）、**关联任务状态芯片**（点击直接打开该任务的详情抽屉）、最近发布时间。初始与 SSE 重连拉取；失败时区保持空、面板照常。

## 状态

- 事件区 UI：**IMPLEMENTED + VERIFIED（真实 Chromium）**
- 事件投影/端点/CLI：第三片已交付（VERIFIED）
- 待办/运行视图：**NOT_RUN（GLM-10 剩余页）**

## 改动文件

- `stock_agent/task_board.py` — `.evsec` 样式、`<details open>` 区块、`showEvents`/`fetchEvents`、任务链点击委托（复用 openDetail）、`window.__taskBoardEvents` 钩子、初始与 onopen 拉取
- `tests/browser_board_check.py` — 4 项新检查：故事渲染、修订徽章+来源、任务芯片点击开抽屉（URL 感知的 fetch 替身：/tasks/ 返回详情夹具，其余返回事件夹具）、折叠切换
- `tests/board_page.html`、`tests/board_states.json` — 快照再生

## 开发期修正（如实）

初版 `<details>` 默认折叠——隐藏内容的 `inner_text()` 返回空且芯片不可点击，浏览器检查当场抓住；改默认 `open`。`Locator.is_open` 不存在，用 `evaluate` 读 `details.open` 属性。

## 实际命令与结果

- 真实浏览器：`python tests/browser_board_check.py` → **22/22 PASS**。
- 全量隔离回归（首跑 572s）：errors=16——**两个 board HTTP 测试**（test_overview 的端点测试、test_task_detail 的端点测试）在重载下 ConnectionRefused（WinError 10061，客户端连早于 serve_forever 就绪）；两模块单跑各 3 次全过、全量复跑（372s）回到 errors=14 干净。**加固**：给三个 board HTTP 测试（overview/detail/events-view）加 /health 就绪轮询（≤5s），模块复验全过。这是当日第三种负载抖动形态（viewer 10053 已修、10061 为首现）——模式：全量套件 ~60 个临时 HTTP 服务器在 Windows 网络栈压力下，就绪类竞态偶发；readiness 轮询是 10061 的正解。日志：glm10evui_regression（16）/rerun（14）/final（加固后）。
- 最终全量（加固后）：**errors=14（既有原生守卫）, skipped=2, failures=0**（以 %TEMP%\glm10evui_final_20261012.log 为准）。
- live/原生/真人：未执行；零模型调用。

## 生产配置、权限、账本变化

无。

## 回滚

还原 task_board.py 增量与检查脚本、git 还原夹具。无数据迁移。

## 遗留与下一批

- GLM-10 剩余：待办视图（按权限）、运行视图（来源健康/额度/门禁）。
- 待用户清单不变：ADR-002、来源批准、vault 事故处置、mimosa 完整审计。
