# K01：Kimi 资产与契约核实清单（2026-10-12）

状态：**离线盘点完成；全部真实授权 BLOCKED；无任何真实查询。**

本文是 [GLM Kimi 接入计划](../GLM_KIMI_INTEGRATION_PLAN_20261012.md) 的 K01 交付。
逐文件 SHA-256 清单见 [KIMI_K01_ASSET_HASHES_20261012.json](KIMI_K01_ASSET_HASHES_20261012.json)。
本文不构成批准记录；"查到了契约"不等于"有调用权"。

## 1. 基线核对

- 工作树核对（2026-10-12）：`git status` 干净（仅本计划文档未跟踪）。计划附录 A 所述
  `budget_guard.py`、`research_worker.py`、`task_board.py`、`task_runtime/store.py` 未提交改动
  已经全部随 GLM-11 落库（HEAD `58710bc`），本轮不需要保护性避让。
- 计划附录 A 四个基准哈希（kimi_gateway.py、mcp_source_server.py、docs/ifind.md、茅台样本 CSV）
  **全部 MATCH**：外部资产自计划编写以来未变动。
- 本轮对两个外部目录为只读访问；未运行网关、未读取任何密钥文件、未安装依赖、未写入外部目录。

## 2. 资产与许可矩阵

许可来源两处：`kimi.plugin.json` 的 `license` 字段（manifest 标示）与根目录 LICENSE 正文（实际文本）。
两者冲突时按更严格者处理，标 `REVIEW_REQUIRED`。

| 包 | 版本 | manifest 标示 | LICENSE 正文 | 初步判定 | 处理 |
| --- | --- | --- | --- | --- | --- |
| institutional-finance-kit | 1.2.7 | MIT | MIT / (c) 2026 Moonshot AI | 一致 | 方法参考首选；子目录与依赖另核 |
| xtt-public-markets-investing | 0.11.5 | MIT | Apache-2.0 / Moonshot AI | **声明冲突** | REVIEW_REQUIRED，不引入 |
| xtt-investment-banking-private-equity | 0.8.4 | UNLICENSED | Apache-2.0 + 第三方例外说明 | 例外未解 | 不引入 |
| xtt-corporate-finance-accounting | 0.7.4 | UNLICENSED | Apache-2.0 + 数据源脚本例外声明 | 例外未解 | 不引入 |
| dynamic-finance-charts | 1.0.17 | MIT | **根目录无 LICENSE 文件** | 来源不明 | 不引入；仅登记 |
| financial-market-terminal | 1.4.0 | MIT | **根目录无 LICENSE 文件** | 来源不明 | 只读契约参考，不复制 |

- 222 个 SKILL.md 的逐文件哈希已落清单 JSON（1+14+22+41+57+87，与计划一致）。
- `kimi-datasources` 全部 18 个文件（含 12 份契约文档、mcp.json、样本 CSV）哈希已落清单 JSON。
- 原始分发来源与提取日期：**unknown**（本地目录无 git 元数据、无下载回执）；权利追溯留给负责人。
- manifest 标示作者字段含个人名（dongju / xutiantian / 董驹），Moonshot AI 版权行覆盖 institutional-finance-kit
  与 xtt-public-markets-investing 两个包；其余包版权行为 "Finance Plugin Suite"。原作者身份与分发链未独立核验。

## 3. 首批 API 契约表

类型化契约快照（K02 验证器的数据源）：[config/candidates/kimi/contracts.v1.json](../../config/candidates/kimi/contracts.v1.json)。
契约转写自 `docs/ifind.md`（SHA-256 `6DA217A0…B49F1D`）与 `docs/dongcai.md`，只做结构化不改语义。

| API | data_kind | 关键参数约束 | PoC 收窄 | 已知缺口（契约未写明） |
| --- | --- | --- | --- | --- |
| ifind_get_price | daily_quote | A 股 ticker ≤3；日期窗 ≤3 年；interval ∈ D/W/M/Q/Y；adjust ∈ forward/backward/none | 1 标的、≤30 天、必须显式 adjust、D 线 | 量单位、币种、源时间戳均未声明 |
| ifind_get_stock_announcement | announcement_metadata | A 股 ticker ≤3；显式日期窗 | 1 标的、≤31 天 | 分页上限、更正关联键未声明 |
| ifind_get_financial_statements | financial_statement | statement 别名规范化 bs/is/cf；报告期 YYYY+0331/0630/0930/1231 | 1 标的、1 张表 | 累计/单季、合并口径、重述版本未声明 |
| dongcai_mx_finance_search | news_metadata | query ≤200 字符；拒绝本地路径/凭据样式文本 | 每事件 1 次 | 响应字段、去重键未声明 |

所有 `file_path` 参数按 F01 处理：契约中标 `virtual_artifact_name` + `host_generated: true`，
调用方传入的任何值都会被拒绝，实际值由宿主适配器生成，不携带真实本地路径。

## 4. 权利矩阵（五项拆分，全部 BLOCKED）

| 维度 | 状态 | 依据缺口 |
| --- | --- | --- |
| 访问（账号可查） | BLOCKED | 无负责人提供的受支持认证方式与独立凭据引用；逆向网关可用性未获服务方确认 |
| 保留（正文落盘） | BLOCKED | 商业使用与正文存储权未审核；会员条款未取证 |
| 外发（发送外部模型） | BLOCKED | 同上；查询问题本身的外发也需最小化审批 |
| 展示（面板渲染） | 部分可行 | 元数据与运行记录可展示；正文/摘录展示待保留权确认 |
| 预算（额度计量） | BLOCKED | 套餐额度、计费口径无官方证据；一切调用记 `cost_unknown` |

结论：K02/K03 只做离线安全适配；live 查询保持关闭，任何测试不得联网。

## 5. Registry 兼容影响

- 候选登记条目：[config/source_registry.kimi.example.json](../../config/source_registry.kimi.example.json)，
  4 条全部 `pending_human_approval`、保留/外发 false，不进生产 `config/source_registry.json`。
- **schema 变更建议（唯一）**：`SOURCE_KINDS` 增加 `financial_statement`。理由：ifind 公告（filing）与三表
  （financial_statement）同 upstream 不同 kind，现有 4 个 kind 无法区分"公告元数据"与"财务事实"，合并会丢失
  权限/时效语义。消费方核对结果：registry kind 仅被 `ops_view` 展示与自校验引用；行情门禁走 signal
  `source_type`，不受影响。fail-closed 语义不变（未知 kind 仍拒绝）。
- upstream 命名：候选条目 upstream 直接写真实上游 `ifind` / `dongcai`（不写 kimi-agent-gw），确保
  同上游多路由不能注册成独立共识源（验收 T16）。dongcai 与既有 eastmoney 直连属同一公司家族，
  批准时须在名册层声明不作为独立共识。

## 6. F01-F07 缺口与本批对应

| 缺口 | K01 结论 | 后续承接 |
| --- | --- | --- |
| F01 远端文件名写盘 | 确认存在（gateway `write_envelope_files` 直接用返回名） | K02 artifacts 受控写器 |
| F02 凭据/出站边界 | 确认存在（桌面 key 自动回退、env 任意 base_url） | K02 transport 固定 origin + 显式凭据引用 |
| F03 契约解析不验证 | 确认存在（object→string、空行切段） | K02 contracts 类型化验证 |
| F04 运行时安装 | 确认存在（wind 脚本缺 SDK 时 pip） | K02 零新依赖；ADR 拒绝整包 SDK |
| F05 协议/失败分类 | 确认存在（error 包在 result、静默吞非法 JSON） | K07 才考虑 MCP；K02 用结构化结果码 |
| F06 资源/共享额度 | 确认存在（无响应上限、秒级文件名碰撞） | K02 上限常量 + 产物 UUID；K03 预算预留 |
| F07 数据类别混同 | 确认存在（东财契约混查询与分析 API） | K03 data_kind 分离 + 白名单拒分析 API |

## 7. 验收对照（K01 自检）

- 资产与许可冲突可定位：是（§2 矩阵 + 逐文件哈希 JSON）。
- 未把"查到契约"写成"有调用权"：是（§4 全 BLOCKED）。
- live 被阻塞时 K02/K03 离线工作可继续：是（K02 不依赖真实 key）。
- 未修改：外部两个目录、共享配置、生产 `config/source_registry.json`、`config/pi_skills*.json`、
  计划任务、自启动。均保持原状。

## 8. 交给负责人的最小事项（不催办，仅登记）

1. 确认 iFinD/东财商业条款下本项目的查询、存储、二次处理权利（§4 三项 BLOCKED 的解锁钥匙）。
2. 确认受支持的认证方式（官方客户端导出 vs 逆向网关），并按 ADR 决定 transport 路线。
3. 批准或否决 `SOURCE_KINDS` 增加 `financial_statement` 的 schema 变更。
4. 追溯 dynamic-finance-charts / financial-market-terminal 的原始分发与 LICENSE 缺失原因（若后续要用）。
