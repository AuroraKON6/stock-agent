# Kimi 方法候选：三项差异（2026-10-12，K04 候选，未激活）

> **与已提交 [KIMI_K04_METHOD_CANDIDATES_20261012.md](KIMI_K04_METHOD_CANDIDATES_20261012.md) 的关系（合并指引）**：
> 那是并行会话产出的第一批候选（已提交，含 tier 阶梯/口径五元组/量级四查/UNSOURCED），
> 本文件是同一外部来源上独立产出的**字段级补充批**。两者关系：
> - **互补**：K04 批管"断言层级与口径纪律"（tier/五元组/派生木桶），本批管"数值字段完整性"
>   （五块字段/单位白名单/scenario 标注/血缘）与"取证操作纪律"（对照期独立取证/坐标回读/as-of 声明）。
> - **重合（合并时择一）**：本批 C2 第 9 条的量级自洽 ≈ K04 批候选 2 第 6 条；本批 C2 第 8 条的
>   累计口径 ≈ K04 批口径五元组的 period_basis。重合条目以措辞更完整者为准，不双写。
> - **来源一致性**：两批同源于 institutional-finance-kit；K04 批记录的三份源文件 SHA-256
>   （morning-briefing `0c9a92ca…`、earnings-review `acbba70c…`、data-registry `d91a33a1…`）
>   与本批引用的 K01 清单一致，可直接沿用。
> 评审时建议两批合并为一份最终差异清单。

状态：**候选差异，等待现有审核机制；未修改 `config/pi_skills.json`、未重写
`config/pi_skills.lock.json`、未改 `.pi/skills/` 生产方法。**

来源：`E:\GitHub上下载的\kimi-finance-skills\institutional-finance-kit`（MIT，manifest 1.2.7，
LICENSE 正文 MIT / (c) 2026 Moonshot AI，声明一致——K01 权利矩阵中唯一许可一致包）。
原文件哈希与逐字节记录见 [KIMI_K01_ASSET_HASHES_20261012.json](KIMI_K01_ASSET_HASHES_20261012.json)
（`roots.kimi-finance-skills.packages.institutional-finance-kit.skills[]` 中 path
`skills/morning-briefing/SKILL.md`、`skills/earnings-review/SKILL.md`、`skills/data-registry/SKILL.md`）。

外部原技能是 31-63 KB 的执行型技能（自动取数、渲染、QA 脚本、凭证发现）。按计划 5.2/6.3，
**只提取方法内核为现有三个项目自有技能的增补步骤候选**，不迁移脚本、hooks、渲染器或任何执行指令。

## 候选文件

| 候选 | 增补目标（现有技能） | 候选文本 |
| --- | --- | --- |
| C1 晨报结构 | `research-synthesis`（research_desk, case_analyst） | [output/skill_audit/kimi_candidates/c1_research_synthesis_addendum.md](../../output/skill_audit/kimi_candidates/c1_research_synthesis_addendum.md) |
| C2 业绩点评纪律 | `financial-quality-review`（research_desk, case_analyst） | [output/skill_audit/kimi_candidates/c2_financial_quality_review_addendum.md](../../output/skill_audit/kimi_candidates/c2_financial_quality_review_addendum.md) |
| C3 数据登记字段 | `source-provenance-audit`（signal_analyst, research_desk, case_analyst, public_handoff） | [output/skill_audit/kimi_candidates/c3_source_provenance_audit_addendum.md](../../output/skill_audit/kimi_candidates/c3_source_provenance_audit_addendum.md) |

每份候选文本含：输入、输出、引用要求、停止条件、适用市场、示例失败（见各文件头部表）。

## 差异摘要（学什么 / 不搬什么）

| 候选 | 学 | 不搬 |
| --- | --- | --- |
| C1 | as-of 截止声明（检索覆盖 ≠ 事实覆盖）；催化/风险事件分层带确认状态；未知项独立区块 | 个人持仓配置、财联社全窗扫描、自动取数 runner、HTML/PDF 渲染、QA 脚本闸 |
| C2 | 对照期独立取证不邻期顶替；数字坐标回读 + 量级自洽；A 股累计口径显式 | 美股 NVDA/AMD 解析器、SEC 固定流程、`python3` 执行指令、卖方预期 API |
| C3 | 数值五块字段（值/单位/报告期/坐标/哈希）；非货币单位白名单；复权必填；scenario/estimate 标注；派生血缘 | 独立登记簿权威（项目权威是 Vault+底稿）、JSON 登记脚本、驳回处置自动化 |

**与现有内容的重复检查**：`source-provenance-audit` 已有"authority/freshness/traceability"骨架，
C3 只补"数值字段级"粒度（五块字段、单位白名单、血缘），不重复 tier 分级；`financial-quality-review`
已有口径一致性要求（第 3、6 条），C2 只补"对照期独立取证"与"坐标回读"两条操作纪律；
`research-synthesis` 已有事实/假设/缺失分层（第 3 条），C1 只补 as-of 声明与事件确认状态。

## 审核检查表（人工逐项，GLM 不代填）

- [ ] 许可保留：institutional-finance-kit MIT（manifest 与 LICENSE 一致）；子文件无额外例外声明（K01 §2）。
- [ ] 来源哈希：三份原文件 SHA-256 已入 K01 清单；激活前重新核对未漂移。
- [ ] 改写为纯文本：候选文本无代码围栏、无脚本、无 URL 抓取、无工具调用（符合 pi_skills
  `PROHIBITED_TEXT_MARKERS`）。激活时按 supply-chain 程序重写为正式增补并登记 provenance
  （`approved_adapted` + pinned commit 需真实上游仓库，本地提取物 unknown 时不得填假 commit）。
- [ ] 角色映射：与目标技能现角色一致，不扩角色。
- [ ] 提示词大小：三段候选合计约 2.4 KB；research_desk bundle 现 8521/48000 字节，余量充足。
- [ ] 零执行权限：候选不新增任何 permission（8 项 denied 保持）。
- [ ] 指纹失效：任何激活都会使旧 lock 失效，须走 `pi-skills-lock --write` 人工确认。

## 评测候选（激活后，非本批）

用既有离线研究 fixture 做 A/B：现有方法 vs 现有+增补，比对引用正确率、单位错误、
未知项表达、输出长度。真实效果进既有价值验证，不伪造提升比例。

## 激活路径（供审核人参考，本批不执行）

1. 审核通过候选文本 → 合并进 `.pi/skills/<id>/SKILL.md`（人工或授权会话）。
2. `config/pi_skills.json` 该技能 source_repositories 若引用本地提取物，须先补真实上游
   repo+commit（当前 unknown，见 K01 §2）。
3. 跑供给链测试 → `pi-skills-lock --write` → 全量测试 + Doctor + UAT。
