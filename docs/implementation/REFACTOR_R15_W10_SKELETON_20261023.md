# R15 第一条个人研究 W10 —— REPLAY 骨架（交付报告）

日期：2026-10-23，Asia/Shanghai。实施者：GLM（第十五批：R15 replay 骨架，用户明确指示「先跑 replay 骨架」）。
任务定义：[任务书 R15](../GLM_REFACTOR_TASKBOOK_20261020.md)。前置 R01—R14 全部已提交（→`93bf14b`）。

## 1. 包 ID 与用户行为

R15-W10-SKELETON。**蓝图纵向切片在 replay 模式下全链贯通**——一个测试驱动真实生产组件走完整个个人研究闭环：

```
个人会话登录（R10）
 → 工作台创建研究（R14）
 → 公告导入个人 namespace（R12a，policy 审计）
 → HTML 正文定位摘录（R12a）→ 类型化事实（R05）
 → 事实进任务 payload → replay worker 运行（R06 质量 + R13 事实包随行）
 → 用户追问补证（A64/R01 快照链 v1→v2）
 → v2 重跑至完成
 → 显式重新研究（A90：新意图+prior 链接，旧结果不动）
 → 个人签收（A50：scope 隔离）
 → 全链可审计（事件/快照/包哈希/导入审计）
```

骨架边界（用户已知情并选择）：公告材料为**明示标注的样本 fixture**（文内即声明「非真实披露」，source_url 带 fixture 标记）；研究执行为 replay（**真实模型调用 NOT_RUN**）；个人签收由测试会话 actor 完成（**真人签收 A69 留待用户在真实 W10 验收时亲自操作——Agent 永不代填**）。

## 2. 起始/结束 HEAD 与改动文件

起始 HEAD `93bf14b`。

| 文件 | 内容 |
| --- | --- |
| `tests/test_refactor_r15_w10_skeleton.py`（新增 1 端到端） | 上述全链，逐阶段断言（§4） |
| `docs/implementation/REFACTOR_R15_W10_SKELETON_20261023.md`（本报告） | 骨架验收 + 真实 W10 升级路径（§5） |

生产代码零改动——这正是骨架的意义：R01–R14 的治理链**无需任何为「打通」而设的特例**即形成闭环。唯一接线发现：个人 namespace 的事实要进事实包，worker 必须拿**个人视图 config**（机构视图看不到个人文档）——这是运行时配置传递事项（真实运行由入口按 owner 组装视图），非代码缺陷。

## 3. 命令、退出码、结果

| 命令 | 结果 |
| --- | --- |
| `--pattern test_refactor_r15_w10_skeleton.py` | 1 test OK（全链） |
| 全量 `run_isolated_tests.py` | **1544 tests, errors=14, skipped=2, failures=0, exit 1**（207.156s；基线 1543/14/2 + 1；错误构成不变=守卫阻断） |

## 4. 骨架各阶段断言（端到端测试内）

| 阶段 | 关键断言 |
| --- | --- |
| 登录/创建 | 会话 200；create RECORDED；task 入队 |
| 个人导入 | 个人根下对象；审计含 institutional_admission=NOT_REQUESTED |
| 正文/事实 | HTML 链版本摘录含「1,234.56 万元」；statement 事实绑定摘录 sha |
| 事实包随行 | model_call.fact_bundle.bundle_sha256 存在；outbound_context.verified_facts[0] 含正文数字；A63 哈希链（R13 已测） |
| 追问（A64） | input_version+1；快照链恰 [v1, v2] |
| v2 完成 | 追问输入后的重跑达 succeeded |
| 重研（A90） | 新任务 origin.prior_task_id=原任务；原任务 output 仍 succeeded |
| 签收（A50） | scope=personal…not_institutional_approval；机构批准键不存在 |
| 审计 | task 事件序列含 created/claimed/human_requested/human_response_recorded/succeeded |

## 5. 状态汇总与真实 W10 升级路径

- **VERIFIED**：全链闭环（replay 模式）、逐阶段断言、全量 1544/14/2。
- **NOT_RUN（真实 W10 的三个替换点，全部只需换料不改码）**：
  1. **真实事件原件**：把 fixture 换成用户提供的官方公告（文件+精确 HTTPS URL）——个人导入入口不变；
  2. **真实模型研究**：execution_mode=live + Pi 配置可用（模型 attempt 走 R02 链/R13 包）；
  3. **真人反馈**：用户本人经网页会话追问/签收（A69——骨架测试中的 actor 是测试会话，真实签收必须用户亲手完成）。
- **BLOCKED**：既有 14 个原生守卫阻断维持。

## 6. 回退

新增仅测试+报告，删除即回退；零生产状态。

## 7. 提交与下一包

本批完成即提交。**P2 阶段（个人闭环）以 replay 骨架形态收口**。后续按任务书序可选：R16（Dify 整链接入，引擎/部署条件仍待用户开启 Docker）、或先补浏览器 B 层验收批（页面按钮+Chromium 走查+R08/R09 C 层，一次环境准备多包兑现）、或等真实 W10 三条件就绪直接跑真案。
