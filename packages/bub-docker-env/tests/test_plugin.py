from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bub
import pytest
from bub_docker_env import environment, plugin
from bub_docker_env.config import DockerEnvConfig
from bub_docker_env.environment import DockerEnvironment, _ContainerPool
from loguru import logger


def _config(**overrides: object) -> DockerEnvConfig:
    return DockerEnvConfig.model_construct(
        **{**DockerEnvConfig().model_dump(), **overrides}
    )


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    yield lines
    logger.remove(sink)


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    monkeypatch.setattr(environment, "pool", _ContainerPool())


@pytest.mark.parametrize(
    ("sessions", "session_id", "expected"),
    [
        ("qq:*", "qq:group:g1", True),
        ("qq:*", "cli:default", False),
        ("qq:*,feishu:*", "feishu:chat", True),
        ("*", "cli:default", True),
        ("", "qq:group:g1", False),
    ],
)
def test_session_patterns(sessions, session_id, expected) -> None:
    assert _config(sessions=sessions).matches(session_id) is expected


def test_every_session_runs_in_docker_by_default() -> None:
    # Installing a sandbox plugin should never silently leave sessions on the host.
    assert DockerEnvConfig().sessions == "*"
    assert DockerEnvConfig().matches("telegram:12345")


def test_plugin_leaves_other_sessions_to_bub(monkeypatch, tmp_path, logs) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(sessions="qq:*"))
    assert plugin.provide_environment("cli:default", tmp_path) is None
    env = plugin.provide_environment("qq:group:g1", tmp_path)
    assert isinstance(env, DockerEnvironment)
    assert env.workspace == "/workspace"
    assert env.resolve_path("notes.md") == "/workspace/notes.md"
    assert any(
        "docker_env.session" in line
        and "qq:group:g1" in line
        and env.container.name in line
        for line in logs
    )


def test_skill_directories_are_mounted_read_only_at_host_paths(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    user_skills = home / ".agents" / "skills"
    user_skills.mkdir(parents=True)
    plugin_skills = tmp_path / "site-packages" / "skills"
    plugin_skills.mkdir(parents=True)
    workspace = tmp_path / "ws"
    (workspace / ".agents" / "skills").mkdir(parents=True)

    fake_skills = SimpleNamespace(
        __path__=[str(plugin_skills), str(workspace / ".agents" / "skills")]
    )
    monkeypatch.setattr(environment.Path, "home", lambda: home)
    monkeypatch.setattr(
        environment.importlib, "import_module", lambda name: fake_skills
    )

    mounts = environment.skill_mounts(workspace.resolve())
    assert mounts == [user_skills.resolve(), plugin_skills.resolve()]

    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=workspace
    )
    joined = " ".join(env.container.run_args())
    assert f"--volume {user_skills.resolve()}:{user_skills.resolve()}:ro" in joined
    assert f"--volume {plugin_skills.resolve()}:{plugin_skills.resolve()}:ro" in joined

    off = DockerEnvironment(
        _config(mount_skills=False), session_id="qq:group:g2", host_workspace=workspace
    )
    assert off.container.run_args().count("--volume") == 1


def test_container_is_locked_down(tmp_path) -> None:
    env = DockerEnvironment(
        _config(user="1000:1000", mount_skills=False),
        session_id="qq:group:g1",
        host_workspace=tmp_path,
    )
    args = env.container.run_args()
    joined = " ".join(args)
    for expected in (
        "--network none",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
        "--memory 1g",
        "--user 1000:1000",
        f"--volume {tmp_path.resolve()}:/workspace",
    ):
        assert expected in joined
    # Only the workspace is mounted (skill mounts are covered separately).
    assert args.count("--volume") == 1
    assert args[-3:] == ["python:3.12-slim", "sleep", "infinity"]


def test_session_scope_gives_each_session_its_own_container(tmp_path) -> None:
    config = _config(scope="session")
    a = DockerEnvironment(config, session_id="qq:group:a", host_workspace=tmp_path)
    b = DockerEnvironment(config, session_id="qq:group:b", host_workspace=tmp_path)
    assert a.container is not b.container
    assert a.container.name != b.container.name


def test_shared_scope_removes_the_container_after_the_last_session(
    tmp_path, monkeypatch
) -> None:
    removed: list[str] = []

    async def fake_remove(self) -> None:
        removed.append(self.name)

    monkeypatch.setattr(environment.Container, "remove", fake_remove)
    config = _config(scope="shared")
    a = DockerEnvironment(config, session_id="qq:group:a", host_workspace=tmp_path)
    b = DockerEnvironment(config, session_id="qq:c2c:u", host_workspace=tmp_path)
    assert a.container is b.container

    asyncio.run(a.close())
    assert removed == []
    asyncio.run(b.close())
    assert removed == [a.container.name]


def test_spawn_runs_in_its_own_process_group(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_started(self) -> None:
        return None

    async def fake_exec(*args, **kwargs):
        seen.append(args)
        return asyncio.subprocess.Process.__new__(asyncio.subprocess.Process)

    monkeypatch.setattr(environment.Container, "ensure_started", fake_started)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=tmp_path
    )

    process = asyncio.run(
        env.spawn("echo hi | wc -c", cwd="/workspace/sub", env={"A": "1"})
    )

    args = list(seen[0])
    name = env.container.name
    assert args[:2] == ["docker", "exec"]
    assert args[args.index("--workdir") + 1] == "/workspace/sub"
    assert args[args.index("--env") + 1] == "A=1"
    tail = args[args.index(name) + 1 :]
    assert tail[:4] == ["setsid", "--wait", "sh", "-c"]
    assert tail[5] == process.pgid_file
    assert tail[6:] == ["bash", "-c", "echo hi | wc -c"]
    # Stopping targets the recorded process group, not just docker exec.
    command = process._group_command("TERM")
    assert command[:3] == ["docker", "exec", name]
    assert command[-1] == process.pgid_file
    assert 'kill -TERM -"$(cat' in command[-2]


def test_missing_docker_cli_is_reported(tmp_path) -> None:
    env = DockerEnvironment(
        _config(docker="/nonexistent/docker"),
        session_id="qq:group:g1",
        host_workspace=tmp_path,
    )
    with pytest.raises(environment.DockerError, match="docker CLI not found"):
        asyncio.run(env.read_text("/workspace/x"))


def test_config_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("BUB_DOCKER_ENV_NETWORK", "bridge")
    monkeypatch.setenv("BUB_DOCKER_ENV_SCOPE", "shared")
    config = DockerEnvConfig()
    assert config.network == "bridge"
    assert config.scope == "shared"
