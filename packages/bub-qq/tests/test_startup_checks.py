from __future__ import annotations

import pytest
from bub import configure
from loguru import logger

from bub_qq.channel import QQChannel


@pytest.fixture
def make_channel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def build(**qq: object) -> QQChannel:
        configure._config_data.clear()
        configure.merge(
            configure._config_data,
            {
                "qq": {
                    "receive_mode": "webhook",
                    "state_file": str(tmp_path / "state.json"),
                    **qq,
                }
            },
        )
        configure._global_config.clear()
        return QQChannel(lambda message: None)

    yield build
    configure._global_config.clear()
    configure._config_data.clear()


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    yield lines
    logger.remove(sink)


def test_allow_users_access_needs_a_list(make_channel) -> None:
    with pytest.raises(RuntimeError, match="allow_users"):
        make_channel(c2c_access="allow_users")._check_security_config()
    make_channel(c2c_access="allow_users", allow_users="u1")._check_security_config()


def test_removed_options_and_unsandboxed_shell_warn(make_channel, logs) -> None:
    make_channel(exec_approval=True, workspace_jail=False)._check_security_config()
    text = "\n".join(logs)
    assert "option=exec_approval is ignored" in text
    assert "option=workspace_jail is ignored" in text
    assert "qq.security.shell_unsandboxed" in text


def test_quiet_when_shell_is_off_or_sandboxed(make_channel, logs) -> None:
    make_channel(group_shell="deny")._check_security_config()
    assert not any("shell_unsandboxed" in line for line in logs)
    make_channel(shell_sandbox="external")._check_security_config()
    assert not any("shell_unsandboxed" in line for line in logs)
    assert any("qq.security.shell_sandbox" in line for line in logs)


def test_unclaimed_bot_logs_a_pairing_code(make_channel, logs) -> None:
    code = make_channel()._admins.bootstrap_code()
    assert code is not None
    assert any(f",qq.claim {code}" in line for line in logs)
    assert make_channel(admin_users="c2c:u1")._admins.bootstrap_code() is None
