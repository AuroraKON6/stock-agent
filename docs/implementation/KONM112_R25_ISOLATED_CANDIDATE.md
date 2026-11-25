# KONM-112 R25 隔离集成候选复跑手册

候选是 sol 的干净集成基线 `e1c75a0db07cd12fd766187496cdf427882dca50` 之上、本任务分支
候选链（`ced2652` 起，返工沿同一提交续写）的提交。镜像只在该分支上固定：不合并、不推送、不发布。

## 固定标识

| 项 | 值 |
| --- | --- |
| 集成基线 | `e1c75a0db07cd12fd766187496cdf427882dca50`（tree `1a7fa3cc580ae296dcf72ee3d6688dd268d80fb7`） |
| 候选提交 | 复跑时的检出 `HEAD`，全 SHA 见交付评论及镜像内 `/app/PROVENANCE.json` 的 `source_commit` |
| 镜像引用 | `stock-agent-research:konm70-r25-<sha 前缀>@sha256:<digest>`，digest 见随评论附件的 `konm112.image_binding.json` |
| 部署绑定 | 仓库内 `deploy/docker/image_binding.json` 保持原值（`acad75a8…0acd`），候选绑定只外置、不入库 |
| 平台 | linux/arm64，Docker Engine 29.9.2 |

原试点镜像 `4517b901…06d02` 的来源 `52bbdf25719558075649ab213278ad434d396034` 是本基线的祖先；本基线
另有 R15 `2a60beb2…`、R23 `62df0fd3…`、R24 `65f2f655…` 三个祖先，R16 `74f4813e…` 已在其中。
`git diff --stat 52bbdf2 e1c75a0` 为 70 个文件、+19813/−445。

## 四项复跑

以下命令均在候选提交的干净检出根目录执行，`IMG` 取上表的 `tag@sha256`，`BINDING` 为外置绑定文件，
`<ev>` 为检出目录之外的新证据目录（已存在则驱动拒绝运行）。

1. 构建与身份（只在需要重新构建时执行；构建会改写 `deploy/docker/image_binding.json`，
   取出后用 `git checkout -- deploy/docker/image_binding.json` 还原）：

   ```bash
   [ -z "$(git status --porcelain)" ] && bash scripts/build_image.sh stock-agent-research:konm70-r25-<sha>
   docker image inspect "$IMG" --format '{{.Id}} {{json .RepoDigests}} {{.Os}}/{{.Architecture}}'
   docker run --rm --network none --read-only --user 999:999 --entrypoint sh "$IMG" \
     -c 'sha256sum /app/PROVENANCE.json; cat /app/PROVENANCE.json'
   ```

   预期：`source_commit` 等于检出 `HEAD`，`source_dirty` 为 `false`，provenance 哈希等于绑定里的值。

2. 受控容器流程（66 步，退出码逐项等于预期，驱动退出 0）：

   ```bash
   R25_IMAGE="$IMG" R25_IMAGE_BINDING="$BINDING" bash scripts/r25_image_drill.sh <ev>/r25-image
   ```

3. 回归（宿主 Python 3.12.15，离线；`test_refactor_r15_w10_skeleton.py` 在 macOS 宿主上会因
   `/var` 符号链接被证据路径守卫拒绝，应在容器内跑）：

   ```bash
   for pattern in test_refactor_r25.py test_refactor_r09.py test_refactor_r24.py \
     test_scheduled_workspace_ops.py test_daily_task_cycle.py test_refactor_r25_image.py \
     test_production_pilot_v24.py; do
     python3.12 -B scripts/run_isolated_tests.py --pattern "$pattern" || echo "FAIL $pattern"
   done
   for pattern in test_refactor_r15_citation_contract.py test_refactor_r15_personal_facts.py \
     test_refactor_r15_w10_skeleton.py test_refactor_r16.py test_r16_dify_compose.py \
     test_refactor_r23.py test_refactor_r23_trace.py; do
     python3.12 -B scripts/run_isolated_tests.py --pattern "$pattern" || echo "FAIL $pattern"
   done
   ```

   容器内形式：`docker run --rm --network none -v "$PWD":/src:ro -w /src --entrypoint python "$IMG" -B scripts/run_isolated_tests.py --pattern <pattern>`。

   `R25_IMAGE_BINDING` 指向外置的候选绑定，部署绑定 `deploy/docker/image_binding.json` 不改；不设时驱动读仓库默认绑定，
   候选摘要与之不符时 `031-provenance` 会以退出 6 失败。

4. R24 排空/恢复 + 跨重启联合证据（一次驱动，含 R24 的 seed、backup、restore、compare 与五组负例）：

   ```bash
   python3.12 scripts/konm112_restart_drill.py --image "$IMG" --commit "$(git rev-parse HEAD)" \
     --binding "$BINDING" --output <ev>/restart
   ```

   预期：最后一行 `PASS`，`summary.json` 的 `verdict` 为 `PASS`、`cleanup_remaining` 为空，`drain_gate`
   的 `queued/leased/running/live_leases/open_prepared/open_dispatched` 全为 0。

   排空门的两组独立负例（各自新证据目录，`--commit` 同上）：

   ```bash
   for mode in running live-lease; do
     python3.12 scripts/konm112_restart_drill.py --image "$IMG" --commit "$(git rev-parse HEAD)" \
       --binding "$BINDING" --negative "$mode" --output <ev>/restart-negative-$mode
   done
   ```

   预期：驱动非零退出（1），`drain-gate` 步退出 9，`steps.tsv` 里没有任何 `source-before`、`backup`、
   `restore` 步，`summary.json` 的 `archive_entries`、`restore_entries`、`backup_steps` 均为空。
   `running`：任务仍为 `running`、租约已过期；`live-lease`：`running=0`、`leased=1` 且租约有效。

## 第 4 项逐步预期

| 步 | 命令要点 | 预期退出 | 证明内容 |
| --- | --- | --- | --- |
| `seed` | `r24_restore_drill.py seed` | 0 | 四种 attempt 状态各带一笔预算预留（上限 8），带引用的个人证据任务，冻结工作流 |
| `drain-in-flight` | `timeout 45 … worker-service --max-tasks 20` | 124 | 排空在途：seed 留下的排队工作流任务被执行一次（Dify 关闭，结果为 failed），worker 空转直到超时 |
| `settle-unsent-prepared` | `settle-unsent` | 0 | 仅预留、确认未发的准备态夹具经公开转换结算：attempt → `failed_known/sent=no`，任务 → `failed`（不重试）；预算预留保留、零新增调用、零消费。有任何 attempt 已派发的任务不动，留给排空门 |
| `drain-gate` | `drain-gate`（只读） | 0 | 同一次只读账本读取，记 `read_at`：`queued`、`leased`、`running`、有效租约（仅 `leased/running` 且 `lease_expires_at` 晚于读取时刻；终态任务的历史时间戳不计）、未闭合的 `prepared/dispatched` attempt 均为 0。非零即中止，其后步骤不执行 |
| `access-log-quiescent` | `cat /app/data/dify/access_log.jsonl` | 0 | 停写时网关访问审计的内容 |
| `writers-exited` | `docker ps -q` 按项目标签 | 0，输出为空 | 无在途写者，记为停写起点（R24 独立夹具的 `seed` 保持原样，R25 路径只多出 `settle-unsent-prepared` 一步） |
| `source-before` | `inventory` | 0 | 源卷逐文件字节数与 SHA-256 |
| `backup` / `restore` | `backup-create` / `restore-drill` | 0 / 0 | 独立恢复到另一空卷，`live_project_modified=false` |
| `positive`、五组 `negative-*`、`positive-after-negatives` | `compare` | 0、各 13、0 | 源/恢复逐行账本、预算投影、工作流制品及引用 |
| `ledger-before-restart` | `ledger` | 0 | 九张表全行、逐 attempt 预留/消费、157 tokens、模拟收据 0.012345、outbox 条目序号/ID/hash（条数以 `summary.json` 的 `outbox_events` 为准） |
| `consumer-deliver-before-restart` | `consume … pre-restart 0` | 0 | 读 38、投递 38 |
| `down-for-restart` … `stopped-after-recreate` | `down`、`up -d --no-build --pull never`、`inspect`、`down` | 全 0 | 停写起止时间；重建容器 `Image` 等于固定镜像 ID |
| `ledger-after-restart`、`read-tail-after-restart` | `ledger`、`read` | 0 / 0，读为空 | 重启本身未改账本，游标之后无新事件 |
| `resubmit-same-intents`、`ledger-after-resubmit` | `resubmit`、`ledger` | 0 / 0 | 五个意图均 `DUPLICATE`、任务 ID 不变，账本与重启前逐字段相等 |
| `consumer-resume-after-restart`、`consumer-replay-from-zero` | `consume` | 0 / 0 | 续读 0 条、投递 0；从头重放读 38 条、全部判重、投递 0 |
| `ledger-restored`、`read-restored-from-zero`、`consumer-restored-*` | 恢复侧同上 | 0 | 恢复库 outbox 与源库序号/ID/hash 一致，收据逐条相同 |
| `worker-after-restart` | `timeout 20 … worker-service --max-tasks 1` | 124 | 空队列不重发：未决 `outcome_unknown` 保持 blocked，无新 attempt |
| `ledger-after-worker` | `ledger` | 0 | 与重启前相等 |
| `access-log-final`、`source-after` | `cat`、`inventory` | 0 | 20 个源文件中 19 个哈希不变（三个 SQLite 库在内）；唯一变化是 `dify/access_log.jsonl` 追加了重提交那次网关访问的审计行，原内容是新内容的前缀 |
| `cleanup-compose`、`remaining-*` | `down --volumes`、按标签清点 | 全 0 | 容器、卷、网络均为 0 |

读取与投递分开记录：`read`、`ledger` 只经 `TaskStore.events_after` 读游标；`consume` 是隔离的幂等
消费者，把「投递」和收据写进自己的 SQLite（`/drill-receipts`，独立临时卷），收据主键为事件 ID。
投递动作本身是本地桩，不代表任何真实外部系统。

## 范围

只用项目自有临时卷、`internal: true` 网络、合成数据和本地桩。不调用真实模型、不真实投递、不碰正式卷、
不涉及 Windows 调度和生产切换。
