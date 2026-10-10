# bub-docker-env

Runs Bub's `bash`, `bash.output`, `bash.kill`, `fs.*` and code-mode `run_code` tools inside Docker containers instead of on the host, through Bub's `provide_environment` hook. It requires Bub 0.5 or later; older Bub would ignore the hook and keep running commands on the host, so the package refuses to install there. Use it when people you do not fully trust can make the agent run commands, for example through a chat channel.

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## What runs where

- By default **every session** runs in a container. Session ids look like `<channel>:<chat id>`, so `sessions: "qq:*"` keeps only QQ chats in Docker while your own `bub chat` / `bub run` stay on the host. The log shows `docker_env.session session_id=… container=…` for each session that gets a container.
- The container mounts **the Bub workspace** at `/workspace`, and skill directories outside it (`~/.agents/skills` and skills shipped by installed plugins) **read-only at their host paths**, so scripts a skill tells the model to run are found. Keep secrets (`~/.bub/config.yml`, API keys, state files) outside the workspace. Tools a skill script needs (Python packages, CLIs) must be in the image.
- Tool semantics stay in Bub: timeouts, background shells, output rendering and hooks are unchanged. Only processes and file access move into the container.

Every container starts with:

| Setting | Default |
| --- | --- |
| network | `none` (no network at all) |
| user | the host user's uid:gid, so files in the workspace keep their owner |
| capabilities | all dropped, `no-new-privileges` |
| limits | 1 GB memory, 1 CPU, 256 processes |
| image | `python:3.12-slim` (needs `bash`, `setsid` and `python3`) |

Containers start on a session's first command and are removed when Bub stops. If Docker is unavailable, commands fail; they never fall back to the host.

## Configuration

In `~/.bub/config.yml` (or as `BUB_DOCKER_ENV_*` environment variables):

```yaml
docker_env:
  sessions: "*"             # comma-separated session id globs, e.g. "qq:*"
  scope: session            # session: one container per session; shared: one for all
  image: python:3.12-slim
  network: none             # none, bridge, or a named Docker network
  memory: 1g
  cpus: "1"
  pids_limit: 256
  user: ""                  # uid:gid; empty uses the host user
  workdir: /workspace
  docker: docker            # the docker CLI to call
  mount_skills: true        # mount skill directories outside the workspace read-only
```

`scope: shared` is cheaper but lets sessions see each other's processes and `/tmp`. `network: bridge` lets commands reach the internet **and your local network**.

## Install

`bub-docker-env` is not on PyPI. With a global Bub install:

```bash
bub install bub-docker-env@main
```

For local development, install the package path into the environment that runs `bub`:

```bash
uv tool install bub --with /path/to/bub-contrib/packages/bub-docker-env
```

Docker (Docker Desktop, OrbStack or Docker Engine) must be running on the host. Linux and macOS hosts are supported.

## Cleanup

Containers are removed when Bub stops normally. After a crash or `kill -9` they keep running; remove them with:

```bash
docker rm -f $(docker ps -q --filter label=bub.docker-env=1)
```

The plugin does not do this at startup, since other Bub instances on the same Docker host may be using their containers.
