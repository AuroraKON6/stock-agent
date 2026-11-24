# R24 停写快照恢复补验（KONM-102）

基线 `dc8a4cffd51663c0b4f1c67b1f5c03d7f3422071`。候选取运行时干净检出的完整 SHA，镜像 ID、镜像与 Git 绑定、清单、目录和静止时间窗写入本轮证据目录。KONM-27 已验收合成个人证据和 outbox，本轮在同一源卷增加带真实版本引用的 R16 冻结制品和四笔合成预算记录。

## 复跑

在候选分支的干净检出运行，`--output` 指向检出外尚不存在的目录。每次生成新的 Compose 项目和隔离网络，使用不同的源、归档、恢复和负例卷。驱动与 R17/R22 共用 Docker 演练锁。构建依赖仓库 R08 锁文件与 Docker 的本地镜像摘要支持。

```sh
candidate=$(git rev-parse HEAD)
evidence_root=$(mktemp -d /tmp/konm102-r24.XXXXXXXX)
python3.12 -B scripts/run_r24_restore_drill.py --commit "$candidate" --output "$evidence_root/container"

for suite in refactor_r24 backup_restore budget_reservation budget_guard dify_deployment; do
  suite_tmp=$(mktemp -d "$evidence_root/$suite.XXXXXXXX")
  TMPDIR="$suite_tmp" python3.12 -B scripts/run_isolated_tests.py --pattern "test_${suite}.py"
  printf '%s exit=%s\n' "$suite" "$?"
done
```

若隔离运行器触发已知的 `dir_fd` 相对路径清理误判，记录基线和候选的失败栈，再使用检出外临时目录运行同一组 `unittest`，验证测试主体与退出码。

```sh
TMPDIR="$evidence_root" python3.12 -B -m unittest discover -s tests -p test_backup_restore.py
```

## 验收口径

- 停写从合成 seed 的一次性进程退出开始。Compose 驱动使用内部网络、零端口和专用卷，API、worker 与 scheduler 没有常驻进程。备份 CLI 使用 SQLite backup，清单同时绑定 JSONL 与制品字节。备份、恢复和负例结束后重新计算源卷文件哈希。源卷和清单在整个演练期间保留，清理时回收临时资源。结论仅适用于停写期间的隔离恢复。跨文件原子快照、真实切换及真实供应商调用均未执行。
- 每个 `research_attempt_started` outbox 事件占一个调用额度，按其持久化时间转换为 Asia/Shanghai 业务日期。通过 `budget_event_id` 关联不同的 `prepared`、`succeeded`、`reconciled`、`outcome_unknown` attempt。成功样本从任务持久化轮次读取 120 输入、37 输出、合计 157 token。已对账样本的 `reconciliation_reason` 与 `invocation_reconciled` outbox payload 均保存带合成标识的 CNY 0.012345 收据文本。调用台账没有原生金额、币种或计费精度字段。已预留、成功、仍未知三条记录的货币值为空，合成文本无需估价换算。仍未知样本的任务保持 blocked，且没有后续派发和重排队事件。
- 冻结制品存于 `data/workflow_artifacts/<DSL SHA-256>/`。恢复侧从自身平台 run 和不可变输入快照解析经演练配置批准的版本，在自身目录重算 manifest 和 DSL 字节哈希，比较清单并运行仓库现有 R16 离线验证器。源和恢复任务库九表全列对照，含 outbox 全序列及 payload。亦复用 R25 的个人证据 capture、内容寻址原件和已交付摘录对账。
- 分别在独立恢复副本更改一笔合成金额（0.012345 → 0.012346）、更改一笔 attempt 状态、删除被引用 DSL、修改一条 outbox payload、移除被引用个人证据对象。每份副本预期退出 13 且指出对应键或文件。原恢复卷复查退出 0，源文件哈希保持相同。
- `commands.json` 保存每步命令、UTC 时间和退出码。镜像与演练摘要见 `image_binding.json`、`summary.json`、`in-container-provenance.stdout`。`manifest-and-restore.stdout` 含完整清单和恢复校验。 `positive.stdout` 含两侧合成原始行、逐笔投影、证据引用和 outbox 全列，可独立复算。 `negative-*.stdout` 含失败定位，`remaining-*.stdout` 用于确认本项目的清理结果。
