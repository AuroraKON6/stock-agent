# Linux 候选镜像

在干净工作树中使用 Python 3.12 和 Docker 构建候选。首次构建前，核对 Dockerfile 锁定的两个基础镜像摘要。

```sh
bash scripts/build_image.sh stock-agent-research:candidate
```

构建生成外部绑定 `deploy/docker/image_binding.json`。镜像内的 `/app/PROVENANCE.json` 记录源码提交和依赖锁哈希。保存绑定文件，并从可信构建结果记录 manifest digest 和 provenance SHA-256。如果 Docker 仅返回 config digest，发布前须取得 manifest digest 并重新绑定。

无论使用本地构建还是拉取的候选，Compose 都按同一个镜像摘要引用运行。设置 `STOCK_AGENT_IMAGE_SHA256` 为 manifest digest 去掉 `sha256:` 的部分，设置 `STOCK_AGENT_PROVENANCE_SHA256` 为记录的 provenance 哈希。绑定文件的默认路径为 `deploy/docker/image_binding.json`，可通过 `STOCK_AGENT_BINDING_FILE` 指向团队保存的独立副本。

```sh
docker compose -f deploy/compose.yaml config --images
docker compose -f deploy/compose.yaml run --rm --no-deps --pull never research-api
```

默认命令打印 CLI 帮助并退出。上述命令用于候选冒烟检查。运行发布审计时，它会比较注入的镜像摘要、期望的 provenance 哈希和只读挂载的绑定文件。
