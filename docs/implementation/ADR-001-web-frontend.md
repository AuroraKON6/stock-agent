# ADR-001：任务面板前端与事件推送的技术选择

日期：2026-09-18。状态：已决定（可由项目负责人推翻）。关联：总计划 §4/§10/§16-W09、接手说明 §6。

## 背景

首个产品切片已有只读任务页（`task_board.py`，stdlib ThreadingHTTPServer，127.0.0.1:8768）。W09 完整版需要 SSE 增量事件、行情图（Lightweight Charts）与 React/TypeScript 前端。规划 §6 要求：新增依赖必须先形成 ADR、说明"现有模块不能解决什么"、并做许可证/Windows/锁兼容审查后才准安装。

## 决策

**第一阶段（当前）：stdlib SSE 扩展现有 task_board。** 在 `task_board.py` 增加 `GET /api/v1/events`：以 chunked `text/event-stream` 推送 outbox 事件（轮询 task_outbox 递增 sequence，间隔 1-2 秒），支持 `Last-Event-ID` 断线补发；仅推任务域公开投影，慢客户端丢弃并要求重取快照。不新增任何依赖，与 workbench 同一套 loopback/CSRF 边界。

理由：任务域事件量小（每任务个位数事件），轮询 SSE 足够；避免在 DEEPSEEK 凭据未恢复、真实价值未验证前引入 FastAPI/uvicorn/pydantic 与整个 Node/React 工具链（安装、锁、许可证、CycloneDX SBOM 重审、三个 Python 环境重验收）。现有 stdlib 方案已能满足"排队/执行/成功/失败/等待人工 + 模型实际状态"的实时可见性。

**第二阶段（触发条件后再启动）：FastAPI + React/TypeScript + Lightweight Charts。** 触发条件：真实 live 调用验收通过（W08），且用户确认日/月预算后进入 M2/M3（20-50 标的近实时、图表、Agent Fleet）。届时需逐项补：pydantic/fastapi/uvicorn 许可证审查、Windows 兼容、`requirements.lock.txt` 重锁、三环境 895+ 测试重跑、release-controls SBOM 重生成；前端单独立仓或 workspace 隔离，不与 Python 锁混编。

## 现有模块不能解决什么（第二阶段时成立）

- 多页面 SPA 路由、组件化图表交互与键盘/无障碍要求——stdlib 服务端渲染不可持续；
- 高频行情推流的并发连接管理——ThreadingHTTPServer 每连接一线程，50 标的×多页签不可靠；
- 类型化 API 契约（OpenAPI）——前后端并行开发需要。

第一阶段这些都不构成瓶颈：单用户、单页面、事件量个位数。

## 后果

- 现在零新依赖、零重验收成本；SSE 实现约 60 行 + 测试。
- 第二阶段迁移时 `GET /api/v1/tasks`、`POST /api/v1/human-responses`、`GET /api/v1/events` 的 URL 契约保持不变，前端可平滑替换。
- 若跳过第一阶段直接上 React，将把"未验证价值的产品"与"未审查的依赖链"叠加，违背规划的逐包验收纪律。
