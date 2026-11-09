# 从零到第一次自动修复

这一篇从「刚拿到一台全新的 Linux 服务器」开始，一直做到「服务器自己把一个挂掉的网站修好」。照着一步一步复制命令就行，每一步都写了应该看到什么。

- **要多久**：大约 15 分钟（大部分时间在下载 Docker）。
- **要准备什么**：一台能用 SSH 登录、有 sudo 权限的 Linux 服务器。实测过的是 Debian 12 和 Ubuntu 24.04，别的发行版命令可能不一样，见 [COMPATIBILITY.md](COMPATIBILITY.md)。
- **不需要**：模型账号、API key、手机。第一次修复用的是「入门演练」：一个按固定步骤行动的模拟智能体，不联网、不花钱。接真实模型放在最后（第 9 步）。
- 这一篇全程在终端里操作，没有需要扫码或者点网页的步骤，所以没有截图。

> 约定：灰色框里的是要你输入的命令，一次复制一整行（或一整块）粘贴进终端，回车。`# 后面的文字` 是说明，不用输入。

## 第 1 步：登录服务器，切换到 root

在你自己的电脑上打开终端（Windows 用 PowerShell，Mac 用「终端」）：

```sh
ssh 你的用户名@服务器地址
```

登录后切换成 root（管理员），后面的命令都要用 root 运行：

```sh
sudo -i
```

**应该看到**：提示符的结尾从 `$` 变成 `#`，例如 `root@my-server:~#`。如果要输入密码，输入的是你这个用户的登录密码（输入时屏幕上不显示，正常）。

## 第 2 步：装基础工具

```sh
apt-get update && apt-get install -y python3 curl ca-certificates
python3 --version
```

**应该看到**：最后一行是 `Python 3.10` 或更高的版本号，例如 `Python 3.12.3`。低于 3.10 的系统太旧，换 Debian 12 / Ubuntu 22.04 或更新的系统。

## 第 3 步：把 release 包放到服务器上并校验

需要两个文件：`server-autoops-0.1.0.tar.gz` 和 `SHA256SUMS`。

- **服务器能上 GitHub**：

  ```sh
  cd /root
  curl -LO https://github.com/rmqg/server-autoops/releases/download/v0.1.0/server-autoops-0.1.0.tar.gz
  curl -LO https://github.com/rmqg/server-autoops/releases/download/v0.1.0/SHA256SUMS
  ```

- **文件在你自己的电脑上**：在**你的电脑**上（不是服务器上）运行 `scp server-autoops-0.1.0.tar.gz SHA256SUMS 你的用户名@服务器地址:/tmp/`，再回到服务器上运行 `mv /tmp/server-autoops-0.1.0.tar.gz /tmp/SHA256SUMS /root/ && cd /root`。

然后校验并解压：

```sh
cd /root
sha256sum -c SHA256SUMS
tar xzf server-autoops-0.1.0.tar.gz
cd server-autoops-0.1.0
```

**应该看到**：`server-autoops-0.1.0.tar.gz: OK`。如果是 `FAILED`，文件下载坏了或者被改过，重新下载，不要继续。

## 第 4 步：先跑离线演示（可以跳过，但推荐）

```sh
./bin/server-autoops demo
```

大约 15 秒。**应该看到**：最后一行是

```
结果：21/21 项通过，用时 15.03 秒
```

（秒数每次不一样。）它在一个临时目录里模拟了整套流程，不碰你服务器上的任何东西。

## 第 5 步：装 Docker

自动修复是通过 Docker 去启动、重启容器的，所以要先装 Docker：

```sh
apt-get install -y docker.io
systemctl enable --now docker
docker version --format '{{.Server.Version}}'
```

**应该看到**：一个版本号，例如 `29.1.3`（Ubuntu 24.04）。如果报 `Cannot connect to the Docker daemon`，再运行一次 `systemctl enable --now docker`。

> 已经按 Docker 官网装过 Docker Engine 的，跳过这一步。

## 第 6 步：安装，回答向导的问题

还在 `/root/server-autoops-0.1.0` 目录里：

```sh
./install.sh
```

它会先检查环境（每一项是 `ok`、`提醒` 或 `缺少`，缺少的后面有「怎么办」），然后安装，再问你三个问题：

```
==> 配置向导（随时可以重新运行：sudo server-autoops setup）

第 1 步：智能体（出故障时由谁来修）
  1) 入门演练：不需要模型。装一个示例白名单，只管理演练容器 quickstart-web，故障时自动把它启动起来  ← 默认
  2) 接真实模型：需要一个 OpenAI 兼容接口的地址和 API key（例如 DeepSeek）
  3) 暂时不修：只记录告警、核对服务是否健康
  4) 保持现在的设置（dsh）
输入编号，直接回车用默认：
```

- 第 1 个问题：输入 `1` 回车（入门演练）。
- 第 2 个问题「Bark 地址」：直接回车跳过。
- 第 3 个问题「时区」：直接回车。

> **没有终端、或者想用脚本安装时**（比如你是通过自动化工具执行命令的，看不到上面的提问）：`./install.sh` 会跳过提问。这时改用答案文件，效果和上面三个回答一样：
>
> ```sh
> cp examples/install.env.example /root/install.env
> chmod 600 /root/install.env
> ./install.sh --config /root/install.env
> ```
>
> 示例答案文件里写的就是 `AUTOOPS_AGENT=quickstart`（入门演练），别的都不改。已经不带 `--config` 装过一遍也没关系，直接再运行这一条，它会保留已有文件、只按答案改设置。

**应该看到**（结尾部分）：

```
==> 向导写入了：
  - 白名单 /etc/server-autoops/policy.json：只授权容器 quickstart-web
  - 智能体：入门演练（agent.kind=mock，不需要模型）
...
  ✔ [docker] Docker main：29.1.3（unix:///var/run/docker.sock）
  ! [services] quickstart-web：容器 quickstart-web 不存在
...
  ✔ [service] systemd：server-autoops active
```

这时 `quickstart-web 容器不存在` 的提醒是正常的，下一步就创建它。别的提醒（没配通知、mock 不会真正分析故障）现在也都可以不管。

## 第 7 步：第一次自动修复

先启动一个演练用的小网站（nginx，约 20 MB），确认它能访问：

```sh
docker run -d --name quickstart-web -p 127.0.0.1:18080:80 nginx:alpine
curl -s -o /dev/null -w "http %{http_code}\n" http://127.0.0.1:18080/
```

**应该看到**：`http 200`。（第一次运行会先下载 nginx 镜像，要等几秒到几十秒。）

现在「弄坏」它：把容器停掉，然后像看门狗那样报一条故障：

```sh
docker stop quickstart-web
server-autoops alert --service quickstart-web --fault-code WEB_DOWN --level failure --title 网站挂了 --summary 演练
```

**应该看到**：第一行是 `quickstart-web`（停掉的容器名），然后是一行以 `{"ok": true, "stream_ok": true` 开头的 JSON，表示告警已经写进去了。

等 10 秒，看看它有没有被修好：

```sh
sleep 10
docker ps --filter name=quickstart-web --format '{{.Names}} {{.Status}}'
curl -s -o /dev/null -w "http %{http_code}\n" http://127.0.0.1:18080/
```

**应该看到**：`quickstart-web Up 几秒`，以及 `http 200`。容器是服务器自己启动回来的。

## 第 8 步：看它做了什么

```sh
server-autoops status
```

**应该看到**：一行 JSON，里面 `"queue"` 部分有 `"resolved": 1`（一条告警已修好）。

看这一次修复的详细记录：

```sh
R=$(ls -td /var/lib/server-autoops/runs/*/ | head -1)
python3 -m json.tool --no-ensure-ascii "$R/result.json"
```

**应该看到**（节选）：

```json
    "actions": [
        {
            "before": {"error": "URLError", "kind": "http", "ok": false, "running": false},
            "container": "quickstart-web",
            "op": "start",
            "result": true
        }
    ],
    "business_verified": true,
    "diagnosis": "入门演练：容器停了，按白名单把它启动起来",
    "status": "repaired",
    "verification": [{"probe": {"http_status": 200, "kind": "http", "ok": true, "running": true}, "target": "quickstart-web"}],
```

读法：修之前探针访问网站失败（`ok: false`）；智能体请求 `start`，broker 核对白名单后执行；之后 broker **自己**访问网站得到 200，所以结论是 `repaired`（修好了）。这就是整个系统的工作方式：智能体只能「请求」，执不执行、算不算修好都由 broker 按白名单和探针决定。

到这里，第一次自动修复就完成了。

## 第 9 步：接真实模型（需要你自己操作）

入门演练只会按固定步骤「把容器启动起来」。要让它真正分析日志、改配置，需要接一个大模型：

1. **准备 API key**：在模型服务商的网站上注册并创建 API key（例如 DeepSeek 开放平台）。这一步要你自己在网页上操作，本项目不提供账号。
2. **重新运行向导**，选 `2`，依次填接口地址（DeepSeek 用默认的 `https://api.deepseek.com/v1`）、模型 ID（默认 `deepseek-chat`）、API key（输入时不显示）：

   ```sh
   server-autoops setup
   ```

   没有终端时，用答案文件：在 `/root/install.env` 里写上 `AUTOOPS_AGENT=dsh`、`AUTOOPS_MODEL_BASE_URL=...`、`AUTOOPS_MODEL=...`、`AUTOOPS_MODEL_API_KEY=...`，然后 `server-autoops setup --answers /root/install.env`。

3. **构建智能体镜像**（要联网下载几百 MB，几分钟）：

   ```sh
   docker build -t local/server-autoops-agent:0.1.0 /opt/server-autoops/agent-image
   docker run --rm local/server-autoops-agent:0.1.0
   ```

   第二条应打印 dsh 的版本号 `0.2.1-rc.5`。

4. **重启服务并自检**：

   ```sh
   systemctl restart server-autoops
   server-autoops doctor
   ```

   **应该看到**：`0 个错误`。剩下的提醒每条后面都写着要不要处理、怎么处理。

5. **再演练一次**：重复第 7 步。这次修复由真实模型完成，`result.json` 里的 `diagnosis` 是模型写的诊断。

## 第 10 步：换成你自己的服务

入门演练的白名单只管 `quickstart-web` 一个容器。正式使用时：

1. 删掉演练容器，清空演练白名单：

   ```sh
   docker rm -f quickstart-web
   printf '{"version": 1, "services": {}, "faults": {}}\n' > /etc/server-autoops/policy.json
   ```

2. 照 [POLICY.md](POLICY.md) 把你自己的服务写进 `/etc/server-autoops/policy.json`，然后 `server-autoops policy check /etc/server-autoops/policy.json`。不用重启服务。
3. 接看门狗，让故障自动报上来：见 [INSTALL.md 第 8 步](INSTALL.md#8-接看门狗)。
4. 手机通知（可选）：iPhone 装 Bark，`server-autoops setup` 里填 Bark 地址，再 `server-autoops notify-test`。

## 卡住了？

1. 先运行 `server-autoops doctor`，每个错误后面都有「修复：」一行，照着做。
2. 再按症状查 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
3. 想从头再来：`/opt/server-autoops/uninstall.sh --purge` 删除程序、配置和数据（不会动你的其他容器），然后回到第 6 步。
