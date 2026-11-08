# 第三方组件

server-autoops 的源码全部是原创（MIT）。仓库**不包含**任何第三方源码文件；下面这些是运行时依赖或构建时从外部获取的组件，均不随本仓库分发。

| 组件 | 用在哪里 | 来源 | 版本 | 许可证 | 怎么获取 |
|---|---|---|---|---|---|
| DeepSeek Harness（dsh）：`@deepseek-ai/dsh`、`@deepseek-ai/dsh-llm-pi-ai` | 智能体运行时（`agent-image/Dockerfile`） | https://github.com/deepseek-ai/deepseek-harness ；npm `@deepseek-ai/dsh` | 0.2.1-rc.5（Dockerfile 里固定） | MIT（npm 元数据 `license: MIT`，2026-11-08 核实；`dsh-base`、`dsh-headless`、`dsh-tools` 同为 MIT） | 用户构建镜像时由 npm 安装 |
| dsh 的传递依赖（例如 `@earendil-works/pi-ai` 等） | 同上 | npm | 由 dsh 的依赖声明决定 | 各自的许可证 | 用户构建镜像时由 npm 安装；本项目不分发 |
| Node.js 官方镜像 `node:24-bookworm-slim` | 智能体镜像的基础镜像 | https://hub.docker.com/_/node | 24（构建时的最新补丁版） | Node.js 为 MIT；镜像内 Debian 软件包为各自许可证 | 用户构建镜像时拉取 |
| Docker Engine / Docker Compose | broker 操作容器、运行智能体 | https://docs.docker.com/ | 用户系统自带 | Apache-2.0 | 用户自行安装 |
| Python 标准库 | 全部程序 | https://www.python.org/ | 3.10+ | PSF License | 用户系统自带 |
| `tomli`（可选，仅 Python 3.10 读写 TOML 配置时） | broker | https://github.com/hukkin/tomli | 任意 | MIT | 用户自行 `pip install tomli` |

`plugin/index.mjs` 导入 `@deepseek-ai/dsh-tools`（MIT）的 `defineTool`，它在运行时由镜像里的 dsh 提供，不随本仓库分发。

通知后端 Bark（https://github.com/Finb/Bark ，MIT）只通过它公开的 HTTP 接口调用，不包含其代码。
