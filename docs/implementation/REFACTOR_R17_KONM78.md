# R17 Dify 受控角色编排验收

KONM-78 从 `da26f2889e99f7dfc410b065df67b37d4e4d8137` 续做 R17。交付对象为一类合成公告事件草稿。Dify 依次请求分析、反证、综合，由 Python worker 做角色执行、引用检查、质量门与 attempt 管理。根任务保存各角色的任务 ID、摘要和输出摘要。

## 复跑

在仓库根目录用 Python 3.12 和 Docker Compose，选择可用的回环端口和一个尚不存在的证据目录。Compose 需支持 `!override`。

```sh
python3.12 -B scripts/r17_role_drill.py all ../r17-replay-01 --port 18179
```

脚本从目录名计算专用 Compose 项目，用生成的凭据、限定案件范围、临时卷和固定镜像运行。在启动前执行 `config --quiet`、`config --images` 和 `pull --policy missing`，再执行 `up -d --pull never --wait --wait-timeout 300`，等待 Celery 就绪。执行时使用服务 API 调用明确的 Dify 发布版本 ID。收尾执行 `down --volumes --timeout 5`，逐项核对项目标签、回环端口和证据目录。需要保留运行环境进行诊断时，可以在同一个证据目录依次执行 `prepare --port 18179`、`up`、`run`、`down`。

在安装仓库锁定的 PyYAML 依赖后运行定向测试。

```sh
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r17.py
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22.py
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r16.py
python3.12 -B scripts/run_isolated_tests.py --pattern 'test_*dify*.py'
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r17_role_api.py
```

## 验收证据

| 项 | 核对位置 |
| --- | --- |
| A76 | `dify-publication.json`、`run-01-main.json` 和 `snapshot-01-main.json` 关联发布版本、Dify run、根和三角色。互斥、步序、案件、任务来源、输入版本和凭据拒绝见 `a76-full-chain-probe.json`、`call-refusal-*.json` 与网络探针。 |
| A77 | `event-role-flow.manifest.json` 将原始 DSL 摘要与冻结角色制品绑定。受控入口校验挂载的 DSL、批准的流程和 run 记录。定向测试检查画布改动及提示词改动拒绝沿用旧批准。实际执行的 prompt 摘要见步骤快照及 `summary.json`。制品中具名批准记录属于合成演练。 |
| A78 | 重试、终态、重复执行及拒绝分支见 SSE 摘要和尝试快照。用同一问题摘要对照整链与角色草稿结构、允许引用集合和质量门。场景账本把执行归到具体步骤 attempt。 |
| A79 | `snapshot-12-before-rollback.json` 到 `snapshot-15-rollback-reconciled.json` 保存切换前后数据。`R17_ROLE_ENTRY=disabled` 拒绝新绑定，已有绑定仍按原版本续跑。新 Dify 根由整链认领，既有成功步骤及其结果摘要留存。 |

O/C 使用真实 Dify 容器、合成公告和预置合成回放。允许引用集合由 worker 的上下文规则计算。本样本集合为空，质量门拒绝无引用的数字陈述。整链消耗一份回放答复，三步路径消耗三份，失败尝试另行对账。本演练的实际供应商请求、token 和实际费用均为零。L 层的 requested/returned、真实用量和成本对比待取得 R17 样本的供应商授权和独立额度。KONM-71 的两次授权均已有历史派发记录。

Dify 应用密钥和受控入口令牌均为一次性演练凭据。Dify 运行在隔离网络，Squid 只允许五个审阅过的 POST 路径，Python 入口监听隔离网关的回环地址。真实调用仍由现有 Python 供应商边界治理。
