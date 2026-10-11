# ADR-002 个人操作身份绑定（草案，未验收）

状态：**DRAFT — 未验收**。验收前浏览器可信写入不扩展；已校验的本地 CLI 决定入口保持为唯一写路径。本 ADR 只描述方案，不批准任何实现切换。

日期：2026-10-12。作者：GLM。决策人：用户（真实本人 UAT 通过后由其宣布 ACCEPTED/REJECTED）。

## 背景与问题

计划 GLM-06 要求"用户可以追问、补证、取消、接受或否定个人研究结果"，且"每次写入验证主体证明、权限、CSRF/Origin、command_id 和 expected_version"。当前事实：

1. 任务板 POST `/api/v1/human-responses` 验证 loopback、Origin、CSRF、expected_version、command_id，但 `actor` 是浏览器提交的**自由字符串**——任何本机进程都能声称任意 actor。
2. `local_identity.py` 已实现 Windows 进程令牌核验（拒绝线程模拟、核验 SID、映射本地名册角色），`task-summary-open` 已用它发放短期只读查看权限——证明本地账号核验可行，但它证明的是"CLI 进程属于某 Windows 账号"，不等于"浏览器提交由真人做出"。
3. 服务进程 SID、浏览器提交的 actor 字符串都不构成真人身份证明（计划原文）。

个人用途需要清晰命名空间与独立签收事件（计划 §1.3），同时不得降低既有机构案件门槛。

## 决策草案

**阶段 0（现状，本 ADR 未验收时）**：写路径仅限已校验本地 CLI（human-response、task-cancel、task-summary）；页面保留既有单 POST 路由（历史行为不回退），但该路由的 actor 字符串仅作为**未核验声明**记录在事件里；页面不新增任何写按钮。

**阶段 1（本 ADR 验收后可实现）：本地账号锚定的个人操作身份**：

1. 写操作（POST human-responses 及未来的取消/追问路由）要求 `X-Operator-Actor` + 一次性操作令牌；令牌只能由已核验 Windows 账号的本地 CLI 调 `task-operator-session`（新）获取：CLI 先跑 `local_identity.authenticate_local_actor`（真实 SID + 名册角色 + 限制名单校验），通过后向服务注册一个绑定 (windows_subject_hash, actor, 用途, 有效期 15 分钟, 单用途 command_id 前缀) 的短令牌。
2. 服务端验证：令牌存在、未过期、用途匹配、windows_subject_hash 与**服务进程自己读到的请求来源无法比对**——loopback 无法可靠区分本机其他进程，这是诚实的边界：令牌防的是"浏览器页面伪造 actor"，不防"本机恶意进程"。本机恶意进程属于 Windows 账号安全域问题，超出本系统边界，明确不声称防御。
3. 每次带令牌写入记录：actor、令牌 id、windows_subject_hash（哈希不存原文）、command_id、expected_version；事件里保留 `identity_basis: "local_windows_account_via_cli_session"` 与 `not_institutional_approval: true`。
4. 个人签收（personal_acknowledgement）、摘要审核（summary review）、机构门禁维持既有独立动作与事件；带令牌的个人签收同样不触碰机构 JSONL 账本。

**明确不做**：浏览器内密码/生物认证、跨机 SSO、不可抵赖签名（计划 §WS20 结论：账号证明不等于真人在场）；机构身份要求真人独立角色，走既有 Business Onboarding 路径。

## 剩余风险

- 同机多账号共享 Windows 会话时，CLI 令牌可能被同会话进程读取（文件权限缓解，不消除）。
- 阶段 1 的"单用途 command_id 前缀"与既有 idempotency_receipts 的交互需设计（令牌绑定 command_id 前缀，重放同 command_id 仍幂等返回原结果）。
- 真人 UAT（GLM-07）必须由用户本人完成：操作针对明确版本、留证；技术演示不冒充 UAT。

## 验收条件（宣布 ACCEPTED 前）

1. 用户阅读并认可阶段 1 边界（特别是"不防本机恶意进程"的诚实声明）。
2. 阶段 1 实现通过隔离正负测试（令牌过期/用途不匹配/重放/伪造 hash）。
3. 真人 UAT：用户本人经 CLI 获取令牌并在页面完成一次真实决定。
4. 浏览器隔离测试覆盖令牌获取→写入→过期→拒绝全链。

任何一条不满足即保持 DRAFT/REJECTED，页面写入不扩展。
