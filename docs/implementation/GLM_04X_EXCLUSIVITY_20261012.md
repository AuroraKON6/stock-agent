# GLM-04 收尾批交付报告（2026-10-12）：单引擎排他规则

执行：GLM。范围：GLM-04 剩余部分——旧 batch 与新 task 引擎的事件流排他。独立小包，与前批（GLM-04 主体）分属两批交付。

## 工作包与具体用户流程

操作员通过 `task_runtime.event_intake_mode`（batch 默认 / task）声明事件流的权威研究引擎：batch 模式下 `agent-run` 照常、`task-rescan` 拒绝；task 模式下 `agent-run` 拒绝（提示改用 task-rescan + task-run-next）、`task-rescan` 照常。同一事件流永远不会被两个引擎重复研判/重复调用模型。用户主动提问任务（question）不受此门限制——它不是事件流，无双跑风险。

## 状态

- 排他门禁：**IMPLEMENTED + VERIFIED（离线）**
- 计划脚本接线（run_daily_research.ps1 按 mode 分派）：**NOT_RUN（待 task 引擎有 worker 常驻与 GLM-05 完成后再接线，本批只做 fail-closed 门禁）**

## 改动文件

- 新增 `stock_agent/execution_mode.py`（模式解析 + 两个 fail-closed 门禁，错误消息指明需改的配置项）
- 修改 `stock_agent/task_intake.py`（rescan 入口调用门禁，包装为 TaskIntakeError）
- 修改 `stock_agent/cli.py`（`_agent_run_once` 入口调用门禁，PermissionError 拒绝）
- 新增测试 `tests/test_execution_exclusivity.py`（10 用例）；更新 `tests/test_task_intake.py` setUp（rescan 测试显式声明 task 模式——新契约的必然结果）

## 正负用例

默认 batch、显式 task、非法模式拒绝；agent-run 两模式正反；有信号无信号一致拒绝（不依赖数据状态）；rescan 两模式正反；换 purpose 名不可绕过 batch 拒绝；question 任务两模式均可用。

## 实际命令与结果

专项 `test_execution_exclusivity.py` 10 OK（实现前确认失败）；`test_task_intake.py` 13 OK（更新 setUp 后）；全量回归见本报告随附记录（与 GLM-05 批共用一次终态回归，见下批报告引用——本批先跑了一次全量：1092+10=1102 项级联验证，结果记录在 %TEMP%\glm04x_regression_20261012.log）。

## 回滚

还原 2 个修改文件、删除 2 个新文件即可；无数据迁移。默认 batch 模式下 `task-rescan` 被拒是唯一行为变化——这正是本批目的。

## 遗留

计划脚本按 mode 分派的接线（`run_daily_research.ps1` 新增 task 分支）待 GLM-05 worker 循环完成后一并做，避免接一个只会拒绝的分支。
