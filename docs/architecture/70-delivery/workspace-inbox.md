# [70-05] workspace_inbox.py — 工作区收件箱

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 306 | models、config | pipeline（run 末尾 sync）、dashboard、reports（章节） |

## 职责
把当轮 JSON 报告同步成"工作区"视图：信号、证据项、评论、研究草稿的收件箱对象，供工作台/看板消费。

## 关键构件
- `sync_workspace_inbox()`（主入口）— 读 `reports/json` 最新报告 → 生成收件箱对象集 → 写 `data/workspace/inbox/` + 投影。
- 输出计数：signals / evidence_items / comments / research_drafts（进 run stats 的 workspace_* 键）。

## 输入 → 输出
写：`data/workspace/inbox/`、`reports/workspace/`。

## 备注 / 风险
异常被 pipeline `_sync_workspace_inbox` 捕获降级为 workspace_inbox_error（不阻断主流程）。
