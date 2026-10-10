# bub-docker-env

通过 Bub 的 `provide_environment` hook，把 `bash`、`bash.output`、`bash.kill`、`fs.*` 以及 code mode 的 `run_code` 放进 Docker 容器里执行，而不是直接在宿主机上运行。适用于不完全可信的人也能让 agent 执行命令的场景，比如聊天渠道。需要 Bub 0.5 及以上：旧版 Bub 会忽略这个 hook，命令仍在宿主机上执行，所以本包在旧版上会拒绝安装。

English documentation: [README.md](./README.md)

## 什么在哪里运行

- 默认**所有会话**都进容器。会话 id 的格式是 `<渠道>:<聊天 id>`，所以设置 `sessions: "qq:*"` 就只让 QQ 聊天进容器，你自己的 `bub chat` / `bub run` 仍在宿主机执行。每个进容器的会话都会输出一条 `docker_env.session session_id=… container=…` 日志。
- 容器挂载 **Bub 工作区**（位置 `/workspace`），以及工作区之外的 skill 目录（`~/.agents/skills` 和已安装插件自带的 skill），后者**只读、按宿主机原路径**挂载，这样 skill 让模型执行的脚本在容器里也能找到。密钥（`~/.bub/config.yml`、API key、状态文件）请放在工作区之外。skill 脚本依赖的 Python 包、命令行工具需要包含在镜像里。
- 工具语义仍由 Bub 负责：超时、后台 shell、输出渲染和 hook 都不变，只是进程和文件访问挪进了容器。

每个容器启动时的默认设置：

| 项目 | 默认值 |
| --- | --- |
| 网络 | `none`（完全断网） |
| 用户 | 宿主机当前用户的 uid:gid，工作区里的文件归属不变 |
| 权限 | 去掉全部 capabilities，并开启 `no-new-privileges` |
| 资源限制 | 内存 1 GB、1 个 CPU、256 个进程 |
| 镜像 | `python:3.12-slim`（需要 `bash`、`setsid` 和 `python3`） |

容器在会话第一次执行命令时启动，Bub 退出时删除。Docker 不可用时命令会直接失败，不会退回到宿主机执行。

## 配置

写在 `~/.bub/config.yml` 中（或使用 `BUB_DOCKER_ENV_*` 环境变量）：

```yaml
docker_env:
  sessions: "*"             # 逗号分隔的会话 id glob，例如 "qq:*"
  scope: session            # session：每个会话一个容器；shared：所有会话共用一个
  image: python:3.12-slim
  network: none             # none、bridge 或某个 Docker 网络名
  memory: 1g
  cpus: "1"
  pids_limit: 256
  user: ""                  # uid:gid；留空使用宿主机当前用户
  workdir: /workspace
  docker: docker            # 调用的 docker 命令
  mount_skills: true        # 以只读方式挂载工作区之外的 skill 目录
```

`scope: shared` 更省资源，但会话之间能看到彼此的进程和 `/tmp`。`network: bridge` 会让命令既能访问外网，**也能访问你的局域网**。

## 安装

`bub-docker-env` 尚未发布到 PyPI。全局安装 Bub 时：

```bash
bub install bub-docker-env@main
```

本地开发时，把包路径装进运行 `bub` 的同一个环境：

```bash
uv tool install bub --with /path/to/bub-contrib/packages/bub-docker-env
```

宿主机上需要有正在运行的 Docker（Docker Desktop、OrbStack 或 Docker Engine）。支持 Linux 和 macOS 宿主机。

## 清理

Bub 正常退出时会删除容器。如果 Bub 崩溃或被 `kill -9`，容器会继续运行，可以这样清理：

```bash
docker rm -f $(docker ps -q --filter label=bub.docker-env=1)
```

插件启动时不会自动清理，因为同一个 Docker 上可能还有其他 Bub 实例在使用自己的容器。
