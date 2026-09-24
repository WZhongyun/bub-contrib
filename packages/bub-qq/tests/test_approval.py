from __future__ import annotations

import asyncio
from pathlib import Path

from bub.channels.message import ChannelMessage
from bub.hooks.interception import ToolCall

from bub_qq import plugin
from bub_qq.approval import PendingApproval
from bub_qq.approval import build_approval_keyboard
from bub_qq.approval import comma_command_to_call
from bub_qq.approval import consume_once_grant
from bub_qq.approval import has_always_grant
from bub_qq.approval import intercept_group_command
from bub_qq.approval import maybe_request_approval
from bub_qq.approval import begin_approval_click
from bub_qq.approval import parse_approval_button
from bub_qq.approval import remember_member_role
from bub_qq.approval import reset_approval_state
from bub_qq.approval import resolve_approval_click
from bub_qq.approval import _execute_pending
from bub_qq.config import QQConfig
from bub_qq.runtime import set_active_channel
from bub_qq.security import QQ_CONTEXT_KEY
from bub_qq.security import QQ_STATE_KEY


class FakeChannel:
    name = "qq"

    def __init__(self) -> None:
        self.messages: list[ChannelMessage] = []
        self.approved_commands: list[PendingApproval] = []

    async def send_for_result(self, message: ChannelMessage) -> dict[str, object]:
        self.messages.append(message)
        return {"id": "sent"}

    async def dispatch_approved_command(self, pending: PendingApproval) -> None:
        self.approved_commands.append(pending)


def _member_state() -> dict:
    return {
        QQ_STATE_KEY: {
            "scope": "group",
            "sender_id": "member-1",
            "sender_name": "Another dream",
            "sender_role": "member",
            "group_openid": "group-1",
            "session_id": "qq:group:group-1",
        },
        "session_id": "qq:group:group-1",
        "_runtime_workspace": "/tmp/ws",
    }


def _config(**overrides: object) -> QQConfig:
    values = {
        "admin_users": "admin-1",
        "group_tool_policy": "restricted",
        "exec_approval": True,
        "workspace_jail": False,
    }
    values.update(overrides)
    return QQConfig.model_construct(**values)


def setup_function() -> None:
    reset_approval_state()
    set_active_channel(None)


def test_approval_keyboard_is_callback_type() -> None:
    keyboard = build_approval_keyboard("abcd1234")
    buttons = keyboard["content"]["rows"][0]["buttons"]
    assert len(buttons) == 3
    assert buttons[0]["action"]["type"] == 1
    assert buttons[0]["action"]["permission"]["type"] == 2
    assert buttons[0]["action"]["data"] == "approve:abcd1234:allow-once"
    assert parse_approval_button(buttons[2]["action"]["data"]) == ("abcd1234", "deny")


def test_member_bash_sends_fixed_keyboard(monkeypatch) -> None:
    import bub

    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config())
    channel = FakeChannel()
    set_active_channel(channel)
    decision = asyncio.run(
        plugin.before_tool_call(
            ToolCall(run_id="r", tool="bash", arguments={"cmd": "uname"}),
            _member_state(),
        )
    )
    assert decision is not None
    assert decision.action == "replace"
    assert "审批" in (decision.result or "")
    assert len(channel.messages) == 1
    outbound = channel.messages[0].context["_qq_outbound"]["keyboard"]
    assert outbound["content"]["rows"][0]["buttons"][0]["action"]["type"] == 1
    assert "Another dream" in (channel.messages[0].content or "")
    assert "member-1" not in (channel.messages[0].content or "")


def test_owner_also_requests_approval(monkeypatch) -> None:
    import bub

    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config())
    channel = FakeChannel()
    set_active_channel(channel)
    state = _member_state()
    state[QQ_STATE_KEY]["sender_role"] = "owner"
    state[QQ_STATE_KEY]["sender_id"] = "owner-1"
    decision = asyncio.run(
        plugin.before_tool_call(
            ToolCall(run_id="r", tool="bash", arguments={"cmd": "uname"}),
            state,
        )
    )
    assert decision is not None
    assert decision.action == "replace"
    assert len(channel.messages) == 1


def test_unauthorized_click_explains_no_permission() -> None:
    async def _run() -> None:
        channel = FakeChannel()
        set_active_channel(channel)
        pending = await maybe_request_approval(
            ToolCall(run_id="r", tool="bash", arguments={"cmd": "uname"}),
            _member_state(),
            _config(),
        )
        assert pending is not None
        button = channel.messages[0].context["_qq_outbound"]["keyboard"]
        approval_id = parse_approval_button(
            button["content"]["rows"][0]["buttons"][0]["action"]["data"]
        )[0]
        plan = begin_approval_click(
            approval_id=approval_id,
            decision="allow-once",
            operator_id="member-1",
            config=_config(),
        )
        assert plan.ack_code == 5
        assert plan.pending is None
        assert plan.notice is not None
        assert "没有审批权限" in plan.notice
        notice = await resolve_approval_click(
            approval_id=approval_id,
            decision="allow-once",
            operator_id="member-1",
            config=_config(),
        )
        assert notice is not None
        assert "没有审批权限" in notice

    asyncio.run(_run())


def test_admin_allow_once_executes(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        channel = FakeChannel()
        set_active_channel(channel)
        remember_member_role("group-1", "owner-1", "owner")
        state = _member_state()
        state["_runtime_workspace"] = str(tmp_path)
        await maybe_request_approval(
            ToolCall(run_id="r", tool="bash", arguments={"cmd": "echo approved"}),
            state,
            _config(),
        )
        button = channel.messages[0].context["_qq_outbound"]["keyboard"]
        approval_id = parse_approval_button(
            button["content"]["rows"][0]["buttons"][0]["action"]["data"]
        )[0]
        notice = await resolve_approval_click(
            approval_id=approval_id,
            decision="allow-once",
            operator_id="admin-1",
            config=_config(),
        )
        assert notice is not None
        assert "已允许一次" in notice
        assert "approved" in notice

    asyncio.run(_run())


def test_execute_pending_returns_error_instead_of_raising(monkeypatch, tmp_path) -> None:
    class BoomExecutor:
        async def execute_async(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr("bub.tools.ToolExecutor", lambda hooks=None: BoomExecutor())
    pending = PendingApproval(
        id="x",
        tool="bash",
        arguments={"cmd": "echo hi"},
        session_id="qq:group:group-1",
        chat_id="group:group-1",
        requester_id="member-1",
        group_openid="group-1",
        preview="bash",
        created_at=0.0,
        workspace=str(tmp_path),
    )

    text = asyncio.run(_execute_pending(pending))

    assert "执行失败" in text
    assert "boom" in text


def test_workspace_escape_requests_approval(monkeypatch, tmp_path) -> None:
    import bub

    monkeypatch.setattr(
        bub,
        "ensure_config",
        lambda cls: _config(workspace_jail=True),
    )
    channel = FakeChannel()
    set_active_channel(channel)
    state = _member_state()
    state["_runtime_workspace"] = str(tmp_path)
    decision = asyncio.run(
        plugin.before_tool_call(
            ToolCall(
                run_id="r",
                tool="fs.read",
                arguments={"path": "/etc/passwd"},
            ),
            state,
        )
    )
    assert decision is not None
    assert decision.action == "replace"
    assert "审批" in (decision.result or "")


def test_owner_can_approve_own_request() -> None:
    async def _run() -> None:
        channel = FakeChannel()
        set_active_channel(channel)
        remember_member_role("group-1", "owner-1", "owner")
        state = _member_state()
        state[QQ_STATE_KEY]["sender_id"] = "owner-1"
        state[QQ_STATE_KEY]["sender_role"] = "owner"
        await maybe_request_approval(
            ToolCall(run_id="r", tool="bash", arguments={"cmd": "echo ok"}),
            state,
            _config(),
        )
        button = channel.messages[0].context["_qq_outbound"]["keyboard"]
        approval_id = parse_approval_button(
            button["content"]["rows"][0]["buttons"][0]["action"]["data"]
        )[0]
        notice = await resolve_approval_click(
            approval_id=approval_id,
            decision="deny",
            operator_id="owner-1",
            config=_config(),
        )
        assert notice is not None
        assert "已拒绝" in notice

    asyncio.run(_run())


def test_always_grant_allows_later_call(monkeypatch) -> None:
    import bub

    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config())
    from bub_qq.approval import _always_grants
    from bub_qq.approval import grant_key

    _always_grants[grant_key("qq:group:group-1", "member-1", "bash")] = None
    assert has_always_grant("qq:group:group-1", "member-1", "bash")
    decision = asyncio.run(
        plugin.before_tool_call(
            ToolCall(run_id="r", tool="bash", arguments={"cmd": "uname"}),
            _member_state(),
        )
    )
    assert decision is None
    assert not consume_once_grant("qq:group:group-1", "member-1", "bash")


def _command_message(content: str = ",tape.info") -> ChannelMessage:
    return ChannelMessage(
        session_id="qq:group:group-1",
        content=content,
        channel="qq",
        chat_id="group:group-1",
        kind="command",
        is_active=True,
        context={
            QQ_CONTEXT_KEY: {
                "scope": "group",
                "sender_id": "owner-1",
                "sender_name": "Owner",
                "sender_role": "owner",
                "group_openid": "group-1",
            }
        },
    )


def test_comma_command_to_call_maps_tape_and_unknown_shell() -> None:
    tape = comma_command_to_call(",tape.info")
    assert tape.tool == "tape.info"
    handoff = comma_command_to_call(",tape.handoff name=phase-1 summary=done")
    assert handoff.tool == "tape.handoff"
    assert handoff.arguments["name"] == "phase-1"
    unknown = comma_command_to_call(",status")
    assert unknown.tool == "bash"
    assert unknown.arguments["cmd"] == "status"


def test_group_comma_command_queues_approval() -> None:
    channel = FakeChannel()
    set_active_channel(channel)
    intercepted = asyncio.run(
        intercept_group_command(
            _command_message(",tape.info"),
            config=_config(),
            workspace=Path("/tmp"),
        )
    )
    assert intercepted is True
    assert len(channel.messages) == 1
    assert "tape.info" in (channel.messages[0].content or "")


def test_group_comma_command_runs_immediately_when_approval_disabled() -> None:
    intercepted = asyncio.run(
        intercept_group_command(
            _command_message(",tape.info"),
            config=_config(exec_approval=False),
            workspace=Path("/tmp"),
        )
    )
    assert intercepted is False


def test_always_grant_skips_comma_command_intercept() -> None:
    from bub_qq.approval import _always_grants
    from bub_qq.approval import grant_key

    _always_grants[grant_key("qq:group:group-1", "owner-1", "tape.info")] = None
    intercepted = asyncio.run(
        intercept_group_command(
            _command_message(",tape.info"),
            config=_config(),
            workspace=Path("/tmp"),
        )
    )
    assert intercepted is False


def test_approved_comma_command_dispatches_real_turn() -> None:
    async def _run() -> None:
        channel = FakeChannel()
        set_active_channel(channel)
        remember_member_role("group-1", "admin-1", "admin")
        intercepted = await intercept_group_command(
            _command_message(",tape.info"),
            config=_config(),
            workspace=Path("/tmp"),
        )
        assert intercepted is True
        button = channel.messages[0].context["_qq_outbound"]["keyboard"]
        approval_id = parse_approval_button(
            button["content"]["rows"][0]["buttons"][0]["action"]["data"]
        )[0]
        notice = await resolve_approval_click(
            approval_id=approval_id,
            decision="allow-once",
            operator_id="admin-1",
            config=_config(),
        )
        assert notice is not None
        assert "已允许一次" in notice
        assert len(channel.approved_commands) == 1
        assert channel.approved_commands[0].command_line == ",tape.info"
        assert channel.approved_commands[0].tool == "tape.info"

    asyncio.run(_run())
