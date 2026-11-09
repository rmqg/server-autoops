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
| debian:12（Debian GNU/Linux 12 bookworm） | arm64 | 3.11.2 / 3.40.1 | OK | 21/21，14.0 s | 成功（`--service` 自动识别为 none） | 4 错误 4 提醒（预期，见下） | 通过 | 64 项 OK | 干净 |
| debian:12 | amd64（仿真） | 3.11.2 / 3.40.1 | OK | 21/21，16.9 s | 成功 | 同上 | 通过 | 64 项 OK | 干净 |
| ubuntu:24.04（Ubuntu 24.04.4 LTS） | arm64 | 3.12.3 / 3.45.1 | OK | 21/21，14.0 s | 成功 | 同上 | 通过 | 64 项 OK | 干净 |
| ubuntu:24.04 | amd64（仿真） | 3.12.3 / 3.45.1 | OK | 21/21，16.9 s | 成功 | 同上 | 通过 | 64 项 OK | 干净 |

doctor 在刚安装、还没配置时报的 4 个错误，和文档说的一致：容器里没有 docker 程序、智能体镜像不存在、`model.env` 还是占位符、`agent.model` 还是示例地址。4 个提醒：policy 没有故障码映射、没有配通知、没有 systemd/procd、状态库还没创建。

## 2. 其他 Python 版本

| 环境 | 内容 | 结果 |
|---|---|---|
| Ubuntu 22.04.5 主机，Python 3.10.12（没有 tomllib） | 单元测试、demo、安装脚本（非 root，`--service none`） | 64 项 OK；21/21 |
| Alpine（`docker:cli` 镜像），Python 3.14.10 | 单元测试、demo | 64 项 OK；21/21 |
| Alpine 3.22，Python 3.12.16 | 单元测试、demo | 64 项 OK；21/21 |

> 第 1、2 节用最终打 tag 的包重跑过；第 3、4 节是在修订（维护模式、socket 目录）之前的包上跑的，修订后的同类检查在第 5 节的真实主机上重做了。

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

## 5. 在真实的 OpenWrt 服务器上替换生产服务

作者自己的 iStoreOS 24.10.8 主机（OpenWrt，内核 6.6.144，x86_64，8 GB 内存，Python 3.11.14，没有 tzdata，Docker 27.3.1，另有第二套 dockerd），用本页的 release 包**替换了原来的内部版本**，并沿用它的 policy（13 个服务、18 个故障码）。全部操作照 [INSTALL.md](INSTALL.md)：`sha256sum -c` → `install.sh --service procd --wait-path <第二套 dockerd 的 socket>`。

| 步骤 | 结果 |
|---|---|
| 影子运行：新版开着维护模式、通知关闭，和旧版并行读同一告警流；写入一条无映射的测试告警 | 两边账本完全一致（同一条 blocked，游标都等于告警流大小） |
| 演练：模拟智能体 + 主 dockerd 上的一次性 64 MiB canary（`docker_smoke.py --maintenance-drill`） | 11/11（含维护模式只记录不修复、关闭时丢弃） |
| 演练：生产智能体配置（dsh 0.2.1-rc.5 + 真实的 OpenAI 兼容模型，智能体在第二套 dockerd 上）修 canary（`docker_smoke.py --agent-json`） | 9/9：模型自己改配置 503→200，容器停止后 start；工具清单只有 `ops`；结束后无残留容器；可用内存最低约 1600 MiB |
| 切换：旧服务 stop+disable，旧账本一致快照接为新账本（表结构相同），procd 启动 | 单实例运行；切换后的实时告警被接住；doctor 0 错误 |
| 生产上的维护模式：on → 写一条只诊断的假告警 → off --drop-pending | 维护期间记录为 pending、0 次运行；关闭后记为 blocked，0 次运行 |

这一轮在真实环境里发现并修好了 4 个问题（已进 CHANGELOG）：broker 的 Unix socket 路径在深目录下超过 108 字节上限；OpenWrt 的 `/run` 是符号链接；安装脚本在没有 tzdata 的系统上写出不可用的 `Asia/Shanghai`；安装脚本把数据盘上的配置目录写进了 `sysupgrade.conf`。

**没有测的**：amd64 的智能体镜像构建（OpenWrt 主机上沿用了已有的同版本 dsh 镜像）；systemd 下的常驻运行；`recreate` 和 `exec` 探针在真实 Docker 上的运行（只有单元测试）；这一轮没有主动推送 Bark（地址文件由 doctor 检查过，第一次真实推送是第二天 08:00 的日报）。见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 6. 新手照文档验收（2026-11-09）

让一个不看源码、不补文档没写的步骤的弱模型（gpt-6-luna，不思考）扮演新手，只照 README 和 [QUICKSTART.md](QUICKSTART.md) 操作。服务器是 Windows 上的一次性 WSL2 发行版（Ubuntu 24.04.5，x86_64，systemd），没有终端，所以安装走的是答案文件 `--config`。

结果：从校验 release 包一直走到第 8 步，第一次自动修复完成：`status` 里 `resolved: 1`，`result.json` 的 `status` 是 `repaired`，broker 探针 HTTP 200。停在第 9 步「去模型服务商网站拿 API key」（需要真人）。它提的两处问题已经改进文档：nginx 刚启动时第一次 `curl` 可能是 `http 000`，要等几秒；安装结束的提示指向 QUICKSTART 第 7 步。接真实模型（第 9 步）和第 10 步没有经过新手验收。

此前作者也在 OrbStack 的全新 Ubuntu 24.04（arm64）上照同样步骤跑过一遍入门演练：报警后约 5 秒容器被启动，结论 `repaired`。

## 自己复现

```sh
python3 -m unittest discover -s tests -t . -v
python3 -I bin/server-autoops.py demo
scripts/build-release.sh && (cd dist && sha256sum -c SHA256SUMS)
# 有 Docker 时，见上面第 1、3、4 节的命令
```
