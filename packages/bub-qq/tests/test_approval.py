from __future__ import annotations

import asyncio
from pathlib import Path

import bub
import pytest
from bub import configure
from bub.channels.message import ChannelMessage
from bub.hooks.interception import ToolCall

from bub_qq import approval
from bub_qq import plugin
from bub_qq.approval import PREVIEW_LIMIT
from bub_qq.approval import begin_approval_click
from bub_qq.approval import build_approval_keyboard
from bub_qq.approval import complete_approval_click
from bub_qq.approval import consume_token
from bub_qq.approval import issue_token
from bub_qq.approval import parse_approval_button
from bub_qq.approval import reset_approval_state
from bub_qq.config import QQConfig
from bub_qq.guard import Requester
from bub_qq.guard import set_registered_admins
from bub_qq.runtime import set_active_channel
from bub_qq.security import QQ_STATE_KEY

ADMIN = Requester(scope="group", sender_id="admin-m", group_openid="group-1")
MEMBER = Requester(scope="group", sender_id="member-m", group_openid="group-1")
SESSION = "qq:group:group-1"


class FakeChannel:
    name = "qq"

    def __init__(self) -> None:
        self.messages: list[ChannelMessage] = []

    async def send_for_result(self, message: ChannelMessage) -> dict[str, object]:
        self.messages.append(message)
        return {"id": "sent"}


def _config(**overrides: object) -> QQConfig:
    values = {
        "admin_users": "group:group-1:admin-m",
        "group_tool_policy": "restricted",
        "c2c_tool_policy": "open",
        "denied_tools": "",
        "group_shell": "approval",
        "c2c_access": "admin_users",
        "state_file": "",
    }
    values.update(overrides)
    return QQConfig.model_construct(**values)


def _state(requester: Requester, workspace: Path, role: str = "member") -> dict:
    return {
        QQ_STATE_KEY: {
            "scope": requester.scope,
            "sender_id": requester.sender_id,
            "sender_name": "Alice",
            "sender_role": role,
            "group_openid": requester.group_openid,
            "session_id": SESSION,
        },
        "session_id": SESSION,
        "_runtime_workspace": str(workspace),
    }


@pytest.fixture(autouse=True)
def _reset():
    reset_approval_state()
    set_registered_admins(())
    set_active_channel(None)
    yield
    reset_approval_state()
    set_active_channel(None)


@pytest.fixture
def config(monkeypatch) -> QQConfig:
    value = _config()
    monkeypatch.setattr(bub, "ensure_config", lambda cls: value)
    return value


def _hook(call: ToolCall, state: dict):
    return asyncio.run(plugin.before_tool_call(call, state))


def _only_pending() -> approval.PendingApproval:
    assert len(approval._pending) == 1
    return next(iter(approval._pending.values()))


# --- keyboard and tokens ---------------------------------------------------


def test_keyboard_targets_admins_and_has_no_always_allow() -> None:
    keyboard = build_approval_keyboard("abc", approver_ids=["admin-m"])
    buttons = keyboard["content"]["rows"][0]["buttons"]
    assert [b["id"] for b in buttons] == ["allow", "deny"]
    for button in buttons:
        assert button["action"]["type"] == 1
        assert button["action"]["permission"] == {
            "type": 0,
            "specify_user_ids": ["admin-m"],
        }
        assert "click_limit" not in button["action"]
    fallback = build_approval_keyboard("abc")["content"]["rows"][0]["buttons"][0]
    assert fallback["action"]["permission"] == {"type": 2}


def test_parse_approval_button() -> None:
    assert parse_approval_button("approve:abc:allow") == ("abc", "allow")
    assert parse_approval_button("approve:abc:deny") == ("abc", "deny")
    assert parse_approval_button("approve:abc:allow-always") is None
    assert parse_approval_button("other:abc:allow") is None


def test_token_is_single_use_exact_and_expires(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(approval, "_clock", lambda: now[0])
    call = ToolCall(run_id="r", tool="bash", arguments={"cmd": "ls"})
    pending = approval.PendingApproval(
        id="p1",
        tool="bash",
        arguments={"cmd": "ls"},
        requester=ADMIN,
        session_id=SESSION,
        chat_id="group:group-1",
        preview="ls",
        created_at=now[0],
    )

    issue_token(pending)
    changed = ToolCall(run_id="r", tool="bash", arguments={"cmd": "ls /"})
    assert not consume_token(SESSION, ADMIN, changed)
    assert not consume_token(SESSION, MEMBER, call)
    assert consume_token(SESSION, ADMIN, call)
    assert not consume_token(SESSION, ADMIN, call)

    issue_token(pending)
    now[0] += approval.APPROVAL_TTL_SECONDS + 1
    assert not consume_token(SESSION, ADMIN, call)


# --- before_tool_call ------------------------------------------------------


def test_admin_shell_posts_full_command_card(config, tmp_path) -> None:
    channel = FakeChannel()
    set_active_channel(channel)
    command = "ls -la && echo `whoami`"

    decision = _hook(
        ToolCall(run_id="r", tool="bash", arguments={"cmd": command}),
        _state(ADMIN, tmp_path),
    )

    assert decision is not None and decision.action == "replace"
    assert "审批" in decision.result
    assert len(channel.messages) == 1
    card = channel.messages[0]
    assert card.chat_id == "group:group-1"
    assert command in card.content
    assert "```\n" + command + "\n```" in card.content
    keyboard = card.context["_qq_outbound"]["keyboard"]
    permission = keyboard["content"]["rows"][0]["buttons"][0]["action"]["permission"]
    assert permission == {"type": 0, "specify_user_ids": ["admin-m"]}


@pytest.mark.parametrize("role", ["member", "admin", "owner"])
def test_non_admin_shell_is_denied_whatever_the_qq_role(config, tmp_path, role) -> None:
    channel = FakeChannel()
    set_active_channel(channel)
    decision = _hook(
        ToolCall(run_id="r", tool="bash", arguments={"cmd": "uname"}),
        _state(MEMBER, tmp_path, role=role),
    )
    assert decision is not None and decision.action == "deny"
    assert channel.messages == []
    assert not approval._pending


def test_over_long_command_is_refused_not_truncated(config, tmp_path) -> None:
    channel = FakeChannel()
    set_active_channel(channel)
    command = "echo safe" + " " * PREVIEW_LIMIT + "; rm -rf ~"
    decision = _hook(
        ToolCall(run_id="r", tool="bash", arguments={"cmd": command}),
        _state(ADMIN, tmp_path),
    )
    assert decision is not None and decision.action == "replace"
    assert decision.result.startswith("Not run")
    assert channel.messages == []
    assert not approval._pending


# --- clicks ------------------------------------------------------------------


class RecordingExecutor:
    calls: list[tuple[str, dict]] = []

    def __init__(self, hooks=None) -> None:
        del hooks

    async def execute_async(self, tool_calls, *, context):
        for tool, arguments in tool_calls:
            RecordingExecutor.calls.append((tool.name, dict(arguments)))

        class _Execution:
            error = None
            tool_results = ["ran"]

        return _Execution()


@pytest.fixture
def executor(monkeypatch):
    RecordingExecutor.calls = []
    monkeypatch.setattr("bub.tools.ToolExecutor", RecordingExecutor)
    return RecordingExecutor


def _request_admin_shell(tmp_path, command="uname -a"):
    set_active_channel(FakeChannel())
    _hook(
        ToolCall(run_id="r", tool="bash", arguments={"cmd": command}),
        _state(ADMIN, tmp_path),
    )
    return _only_pending()


def test_non_admin_click_is_rejected_and_keeps_request(config, tmp_path, executor) -> None:
    pending = _request_admin_shell(tmp_path)
    owner = Requester(scope="group", sender_id="owner-m", group_openid="group-1")

    plan = begin_approval_click(
        approval_id=pending.id, decision="allow", operator=owner, config=config
    )

    assert plan.ack_code == approval.ACK_NO_PERMISSION
    assert asyncio.run(complete_approval_click(plan)) is None
    assert executor.calls == []
    assert pending.id in approval._pending


def test_admin_allow_runs_exactly_the_shown_call(config, tmp_path, executor) -> None:
    pending = _request_admin_shell(tmp_path, "uname -a")

    plan = begin_approval_click(
        approval_id=pending.id, decision="allow", operator=ADMIN, config=config
    )
    notice = asyncio.run(complete_approval_click(plan))

    assert plan.ack_code == approval.ACK_OK
    assert executor.calls == [("bash", {"cmd": "uname -a"})]
    assert notice is not None and "已允许" in notice and "ran" in notice
    # The token was spent by that execution.
    call = ToolCall(run_id="r", tool="bash", arguments={"cmd": "uname -a"})
    assert not consume_token(SESSION, ADMIN, call)
    # A second tap finds nothing to approve.
    again = begin_approval_click(
        approval_id=pending.id, decision="allow", operator=ADMIN, config=config
    )
    assert again.ack_code == approval.ACK_FAILED


def test_deny_does_not_execute(config, tmp_path, executor) -> None:
    pending = _request_admin_shell(tmp_path)
    plan = begin_approval_click(
        approval_id=pending.id, decision="deny", operator=ADMIN, config=config
    )
    notice = asyncio.run(complete_approval_click(plan))
    assert "已拒绝" in notice
    assert executor.calls == []


def test_expired_request_cannot_be_approved(config, tmp_path, executor, monkeypatch) -> None:
    pending = _request_admin_shell(tmp_path)
    monkeypatch.setattr(
        approval, "_clock", lambda: pending.created_at + approval.APPROVAL_TTL_SECONDS + 1
    )
    plan = begin_approval_click(
        approval_id=pending.id, decision="allow", operator=ADMIN, config=config
    )
    assert plan.ack_code == approval.ACK_FAILED
    assert plan.notice == "审批已过期。"
    assert executor.calls == []


def test_execution_rechecks_guard(tmp_path, executor, monkeypatch) -> None:
    current = {"config": _config()}
    monkeypatch.setattr(bub, "ensure_config", lambda cls: current["config"])
    pending = _request_admin_shell(tmp_path)
    # The deployer turned shell off before the admin tapped allow.
    current["config"] = _config(group_shell="deny")

    plan = begin_approval_click(
        approval_id=pending.id, decision="allow", operator=ADMIN, config=current["config"]
    )
    notice = asyncio.run(complete_approval_click(plan))

    assert executor.calls == []
    assert "未执行" in notice


# --- comma commands through the real channel ---------------------------------


class InteractionOpenAPIStub:
    def __init__(self) -> None:
        self.acks: list[dict[str, object]] = []

    async def put_interaction(self, *, interaction_id, code=0, data=None):
        self.acks.append({"id": interaction_id, "code": code})
        return {}

    async def aclose(self) -> None:
        return None


@pytest.fixture
def channel_env(tmp_path, monkeypatch):
    from bub_qq.channel import QQChannel

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    configure.merge(
        configure._config_data,
        {
            "qq": {
                "receive_mode": "webhook",
                "admin_users": "group:group-1:admin-m,c2c:u-admin",
                "state_file": str(tmp_path / "state.json"),
            }
        },
    )
    configure._global_config.clear()
    received: list[ChannelMessage] = []

    async def handler(message: ChannelMessage) -> None:
        received.append(message)

    channel = QQChannel(handler)
    sent: list[ChannelMessage] = []

    async def fake_send(message: ChannelMessage) -> dict[str, object]:
        sent.append(message)
        return {"id": "sent"}

    channel.send_for_result = fake_send
    channel._openapi = InteractionOpenAPIStub()
    yield channel, received, sent
    configure._global_config.clear()
    configure._config_data.clear()


def _group_command(content: str, sender: str = "admin-m", message_id: str = "m1") -> dict:
    return {
        "id": f"event-{message_id}",
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "author": {"member_openid": sender, "member_role": "owner"},
            "content": f"<@bot> {content}",
            "id": message_id,
            "group_openid": "group-1",
            "timestamp": "2099-01-01T00:00:00+00:00",
            "mentions": [{"member_openid": "bot", "is_you": True}],
        },
    }


def _click(approval_id: str, operator: str, decision: str = "allow") -> dict:
    return {
        "op": 0,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": f"interaction-{operator}",
            "type": 11,
            "scene": "group",
            "group_openid": "group-1",
            "group_member_openid": operator,
            "data": {
                "type": 11,
                "resolved": {"button_data": f"approve:{approval_id}:{decision}"},
            },
        },
    }


def test_group_shell_command_waits_for_admin_tap(channel_env) -> None:
    channel, received, sent = channel_env

    async def _run() -> None:
        await channel._handle_transport_payload(_group_command(",ls -la"))
        assert received == []
        assert len(sent) == 1 and "ls -la" in sent[0].content
        pending = _only_pending()

        # A group owner who is not a configured admin cannot approve.
        await channel._handle_transport_payload(_click(pending.id, "owner-m"))
        assert channel._openapi.acks[-1]["code"] == approval.ACK_NO_PERMISSION
        assert received == []

        await channel._handle_transport_payload(_click(pending.id, "admin-m"))
        assert channel._openapi.acks[-1]["code"] == approval.ACK_OK
        assert [m.content for m in received] == [",ls -la"]
        assert received[0].kind == "command"

        # Replaying the approved command needs a fresh approval.
        await channel.dispatch_approved_command(pending)
        assert len(received) == 1

    asyncio.run(_run())


def test_group_owner_command_is_plain_text(channel_env) -> None:
    channel, received, sent = channel_env
    asyncio.run(
        channel._handle_transport_payload(_group_command(",ls", sender="owner-m"))
    )
    assert len(received) == 1 and received[0].kind == "normal"
    assert sent == []


def test_protected_and_plain_commands(channel_env) -> None:
    channel, received, sent = channel_env

    async def _run() -> None:
        await channel._handle_transport_payload(_group_command(",fs.read .env", message_id="a"))
        assert received == []
        assert "命令未执行" in sent[-1].content

        await channel._handle_transport_payload(_group_command(",qq.version", message_id="b"))
        assert [m.content for m in received] == [",qq.version"]

    asyncio.run(_run())


def test_c2c_admin_shell_command_asks_in_private_chat(channel_env) -> None:
    channel, received, sent = channel_env
    asyncio.run(
        channel._handle_transport_payload(
            {
                "id": "event-c1",
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "author": {"user_openid": "u-admin"},
                    "content": ",uptime",
                    "id": "c1",
                    "timestamp": "2099-01-01T00:00:00+00:00",
                },
            }
        )
    )
    assert received == []
    assert len(sent) == 1 and sent[0].chat_id == "c2c:u-admin"
    assert _only_pending().requester == Requester(scope="c2c", sender_id="u-admin")
