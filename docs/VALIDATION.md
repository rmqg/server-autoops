# 实测记录（v0.1.0）

日期：2026-11-08（UTC+8）。所有测试都在合成环境里进行：没有连接任何真实业务服务，没有调用任何真实模型，没有往任何设备推送通知。

测试用的 release 包由 `scripts/build_release.py` 在作者的 Mac（macOS 27.2，arm64）上从 git 索引构建：

```
SOURCE_DATE_EPOCH=1794096000 python3 scripts/build_release.py
```

> 打 tag 时的正式包由 GitHub Actions 用同一个脚本构建，SHA256 以 release 页面的 `SHA256SUMS` 为准；本页记录的是测试用的本地构建。

可复现性：从本地 tag `v0.1.0` 克隆后，在 Mac（只跑 `build_release.py`）和 Alpine 容器（Python 3.14.10，完整跑 `scripts/build-release.sh`：测试 + demo + 打包）里分别构建，得到的 `server-autoops-0.1.0.tar.gz` SHA256 完全相同。构建时间戳取 HEAD 提交时间（或 `SOURCE_DATE_EPOCH`），文件模式取 git 索引，与文件系统无关。

## 1. 照文档在全新容器里安装

宿主：OrbStack（Docker 29.4.0，内核 7.0.14-orbstack），Apple Silicon。脚本 [`scripts/container-validate.sh`](../scripts/container-validate.sh) 一步一步执行 [INSTALL.md](INSTALL.md)：装 python3 → `sha256sum -c` → 解包 → `demo` → `./install.sh` → `doctor` → 放入最小 policy 并 `policy check` → `alert` + `status` → 单元测试 → `uninstall.sh --purge`。

```sh
for img in debian:12 ubuntu:24.04; do for arch in arm64 amd64; do
  docker run --rm --platform linux/$arch -v "$PWD/dist:/dist:ro" -v "$PWD/scripts:/s:ro" $img sh /s/container-validate.sh
done; done
```

| 镜像 | 架构 | Python / SQLite | 校验和 | demo | 安装 | doctor | policy check | 单元测试 | 卸载 |
|---|---|---|---|---|---|---|---|---|---|
| debian:12（Debian GNU/Linux 12 bookworm） | arm64 | 3.11.2 / 3.40.1 | OK | 21/21，14.0 s | 成功（`--service` 自动识别为 none） | 4 错误 4 提醒（预期，见下） | 通过 | 57 项 OK | 干净 |
| debian:12 | amd64（仿真） | 3.11.2 / 3.40.1 | OK | 21/21，18.5 s | 成功 | 同上 | 通过 | 57 项 OK | 干净 |
| ubuntu:24.04（Ubuntu 24.04.4 LTS） | arm64 | 3.12.3 / 3.45.1 | OK | 21/21，14.9 s | 成功 | 同上 | 通过 | 57 项 OK | 干净 |
| ubuntu:24.04 | amd64（仿真） | 3.12.3 / 3.45.1 | OK | 21/21，18.4 s | 成功 | 同上 | 通过 | 57 项 OK | 干净 |

doctor 在刚安装、还没配置时报的 4 个错误，和文档说的一致：容器里没有 docker 程序、智能体镜像不存在、`model.env` 还是占位符、`agent.model` 还是示例地址。4 个提醒：policy 没有故障码映射、没有配通知、没有 systemd/procd、状态库还没创建。

## 2. 其他 Python 版本

| 环境 | 内容 | 结果 |
|---|---|---|
| Ubuntu 22.04.5 主机，Python 3.10.12（没有 tomllib） | 单元测试、demo、安装脚本（非 root，`--service none`） | 57 项 OK；21/21 |
| Alpine（`docker:cli` 镜像），Python 3.14.10 | 单元测试、demo | 57 项 OK；21/21 |

## 3. 真实 Docker 冒烟（broker 操作真容器）

[`scripts/docker_smoke.py`](../scripts/docker_smoke.py) 起一个 64 MiB 的一次性 canary 容器（`python:3.12-alpine`，**只在启动时**读配置，所以必须真的重启才会好），然后通过 scheduler → runner → 模拟智能体 → broker 修复它。在 `docker:cli` 容器里运行（挂载宿主 Docker socket，和 canary 在同一个 Docker 网络）：

```sh
docker network create sa-net
docker run --rm --name sa-smoke-runner --network sa-net -v /var/run/docker.sock:/var/run/docker.sock -v "$V:$V" docker:cli \
  sh -c "apk add -q python3 && python3 -I $V/server-autoops-0.1.0/scripts/docker_smoke.py --workdir $V/smoke --network sa-net --docker-bin /usr/local/bin/docker"
```

Docker 29.4.0 arm64，Python 3.14.10，结果 9/9：

- 修复前 HTTP 503；智能体改 `enabled=true` → broker 真的 `docker restart` → 探针 200 → `repaired`；容器 `StartedAt` 确实变化
- `docker logs` 里的 `api_key=...` 交给智能体前已脱敏；配置文件权限保持 0640
- `docker stop` 之后 → `docker start` → `repaired`
- 故意改坏（`enabled=false`）→ 30 秒探针不通过 → 恢复原文件、再重启 → 200，报 `patch failed; original configuration restored`
- 名单外目标被拒绝；结束时 canary 已删除

## 4. 真实 dsh 智能体（假模型）

镜像构建（arm64）：

```sh
docker build -t local/server-autoops-agent:0.1.0 agent-image     # 成功，约 247 MB
docker run --rm local/server-autoops-agent:0.1.0                 # 0.2.1-rc.5
docker image inspect local/server-autoops-agent:0.1.0 --format '{{json .Config.Labels}}'   # 三个 com.docker.compose.* 都是空串
```

[`scripts/dsh_smoke.py`](../scripts/dsh_smoke.py) 用 runner 的 `kind: dsh` 真正启动智能体容器，模型地址指向脚本里的一个**本地假 OpenAI 兼容服务**（按脚本依次要求 `ops inventory`、`ops inspect`，再用文字收尾）。在 `docker:cli` 容器里运行，智能体容器通过 Docker 网络 `sa-net` 访问假模型。结果 6/6：

- dsh 每次请求模型时，提供的工具列表都恰好是 `["ops"]`（3 次请求）
- `ops inventory` 经插件和 Unix socket 到达 broker，返回本次告警授权的目标
- 插件日志里有 `AUTOOPS_TOOLS=["ops"]`（工具清单核对通过）
- dsh 退出码 0；最终结论由 broker 探针给出：`healthy`、`verified=true`
- 运行结束后没有残留的 `server-autoops-agent` 容器

**没有测的**：amd64 的智能体镜像构建；接真实模型的修复运行；systemd 和 OpenWrt procd 下的常驻运行；真实 Bark 推送；`recreate` 和 `exec` 探针在真实 Docker 上的运行（只有单元测试）。见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 自己复现

```sh
python3 -m unittest discover -s tests -t . -v
python3 -I bin/server-autoops.py demo
scripts/build-release.sh && (cd dist && sha256sum -c SHA256SUMS)
# 有 Docker 时，见上面第 1、3、4 节的命令
```
