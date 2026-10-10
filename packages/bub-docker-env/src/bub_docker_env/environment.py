"""A Bub :class:`~bub.environment.Environment` that runs everything in a Docker container.

Every process is a ``docker exec`` into a long-lived container that has the
host workspace mounted, plus skill directories read-only. Inside the container each process starts its own
session (``setsid``) and records its process group id, so stopping it can
reach the whole tree the way Bub's host environment does with ``killpg``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from bub.environment import CallTool, Environment, Process
from loguru import logger

from .config import DockerEnvConfig

# Runs as `sh -c WRAPPER <pgid-file> <argv...>` inside `setsid -w`: the shell is
# the new session's leader, so its pid is the process group id.
_WRAPPER = 'echo $$ > "$0" && exec "$@"'
# The image's /bin/sh is often dash, whose kill rejects `--` before a negative pid.
_SIGNAL_GROUP = 'kill -{signal} -"$(cat "$0")" 2>/dev/null'


class DockerError(RuntimeError):
    """A docker command failed."""


async def _docker(
    config: DockerEnvConfig, *args: str, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            config.docker,
            *args,
            stdin=asyncio.subprocess.PIPE
            if stdin is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise DockerError(f"docker CLI not found: {config.docker}") from exc
    stdout, stderr = await process.communicate(stdin)
    return process.returncode or 0, stdout, stderr


def skill_mounts(host_workspace: Path) -> list[Path]:
    """Skill directories outside the workspace that Bub loads skills from.

    Mounted read-only at their host paths, so paths in a SKILL.md resolve in
    the container too. Skills in the workspace come with the workspace mount.
    """

    roots = [Path.home() / ".agents" / "skills"]
    try:
        # Plugins ship skills in the ``skills`` namespace package, as Bub reads them.
        roots += [Path(path) for path in importlib.import_module("skills").__path__]
    except ImportError:
        pass
    mounts: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if (
            resolved.is_dir()
            and not resolved.is_relative_to(host_workspace)
            and resolved not in mounts
        ):
            mounts.append(resolved)
    return mounts


class Container:
    """One long-lived container, started on first use and removed when released."""

    def __init__(
        self, name: str, config: DockerEnvConfig, host_workspace: Path, label: str
    ) -> None:
        self.name = name
        self.config = config
        self.host_workspace = host_workspace
        self.label = label
        self._started = False
        self._lock = asyncio.Lock()

    def run_args(self) -> list[str]:
        config = self.config
        user = config.user or (
            f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else ""
        )
        args = [
            "run", "--detach", "--rm", "--init",
            "--name", self.name,
            "--label", "bub.docker-env=1",
            "--label", f"bub.docker-env.key={self.label}",
            "--network", config.network,
            "--memory", config.memory,
            "--cpus", config.cpus,
            "--pids-limit", str(config.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--env", "HOME=/tmp",
            "--volume", f"{self.host_workspace}:{config.workdir}",
            "--workdir", config.workdir,
        ]  # fmt: skip
        if user:
            args += ["--user", user]
        if config.mount_skills:
            for root in skill_mounts(self.host_workspace):
                args += ["--volume", f"{root}:{root}:ro"]
        return [*args, config.image, "sleep", "infinity"]

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._started:
                return
            # A container left behind by a crashed run would block the name.
            await _docker(self.config, "rm", "--force", self.name)
            code, _, stderr = await _docker(self.config, *self.run_args())
            if code != 0:
                raise DockerError(
                    f"could not start container {self.name}: {stderr.decode(errors='replace').strip()}"
                )
            self._started = True
            logger.info(
                "docker_env.container.started name={} key={} image={} network={}",
                self.name,
                self.label,
                self.config.image,
                self.config.network,
            )

    async def remove(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
            await _docker(self.config, "rm", "--force", self.name)
            logger.info(
                "docker_env.container.removed name={} key={}", self.name, self.label
            )


class _ContainerPool:
    """Containers shared by environments, removed when the last one closes."""

    def __init__(self) -> None:
        self._containers: dict[str, tuple[Container, int]] = {}

    def acquire(
        self, key: str, config: DockerEnvConfig, host_workspace: Path
    ) -> Container:
        entry = self._containers.get(key)
        if entry is None:
            name = "bub-env-" + hashlib.sha256(key.encode()).hexdigest()[:12]
            entry = (Container(name, config, host_workspace, key), 0)
        container, users = entry
        self._containers[key] = (container, users + 1)
        return container

    async def release(self, key: str) -> None:
        entry = self._containers.get(key)
        if entry is None:
            return
        container, users = entry
        if users > 1:
            self._containers[key] = (container, users - 1)
            return
        del self._containers[key]
        await container.remove()


pool = _ContainerPool()


class DockerProcess(Process):
    """A ``docker exec`` process; signals reach its process group in the container."""

    def __init__(
        self, process: asyncio.subprocess.Process, container: Container, pgid_file: str
    ) -> None:
        self.process = process
        self.container = container
        self.pgid_file = pgid_file

    @property
    def stdout(self) -> asyncio.StreamReader:
        assert self.process.stdout is not None
        return self.process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        assert self.process.stderr is not None
        return self.process.stderr

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> int:
        return await self.process.wait()

    async def write_stdin(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    def close_stdin(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()

    def _group_command(self, signal: str) -> list[str]:
        script = _SIGNAL_GROUP.format(signal=signal)
        return [
            self.container.config.docker,
            "exec",
            self.container.name,
            "sh",
            "-c",
            script,
            self.pgid_file,
        ]

    def signal(self, *, kill: bool) -> None:
        # Interface is synchronous and must not raise: fire and forget.
        try:
            subprocess.Popen(
                self._group_command("KILL" if kill else "TERM"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning(
                "docker_env.signal_failed name={} error={}", self.container.name, exc
            )

    def is_running(self) -> bool:
        if self.returncode is None:
            return True
        # The shell may have exited before background children it started.
        try:
            result = subprocess.run(
                self._group_command("0"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0


class DockerEnvironment(Environment):
    """Run a session's processes and file access inside a Docker container."""

    python = "python3"

    def __init__(
        self, config: DockerEnvConfig, *, session_id: str, host_workspace: Path
    ) -> None:
        self.config = config
        self.workspace = config.workdir
        self._key = session_id if config.scope == "session" else "shared"
        self.container = pool.acquire(self._key, config, host_workspace.resolve())

    async def spawn(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> DockerProcess:
        await self.container.ensure_started()
        argv = ["bash", "-c", command] if isinstance(command, str) else list(command)
        pgid_file = f"/tmp/.bub-{uuid.uuid4().hex}.pgid"
        args = [
            self.config.docker,
            "exec",
            "--interactive",
            "--workdir",
            cwd or self.config.workdir,
        ]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        args += [
            self.container.name,
            "setsid",
            "--wait",
            "sh",
            "-c",
            _WRAPPER,
            pgid_file,
            *argv,
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return DockerProcess(process, self.container, pgid_file)

    async def read_text(self, path: str) -> str:
        await self.container.ensure_started()
        code, stdout, stderr = await _docker(
            self.config, "exec", self.container.name, "cat", "--", path
        )
        if code != 0:
            raise FileNotFoundError(stderr.decode(errors="replace").strip() or path)
        return stdout.decode("utf-8")

    async def write_text(self, path: str, content: str) -> None:
        await self.container.ensure_started()
        script = 'mkdir -p -- "$(dirname -- "$0")" && cat > "$0"'
        code, _, stderr = await _docker(
            self.config, "exec", "--interactive", self.container.name, "sh", "-c", script, path,
            stdin=content.encode("utf-8"),
        )  # fmt: skip
        if code != 0:
            raise OSError(
                stderr.decode(errors="replace").strip() or f"could not write {path}"
            )

    async def run_code(
        self,
        code: str,
        *,
        tools: Sequence[str],
        call_tool: CallTool,
        write: Callable[[str], None],
    ) -> None:
        from bub.builtin.codemode.code_runner import run_code_in_subprocess

        await run_code_in_subprocess(
            self, code, tools=tools, call_tool=call_tool, write=write
        )

    async def close(self) -> None:
        await pool.release(self._key)
