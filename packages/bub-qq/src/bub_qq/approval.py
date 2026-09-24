"""Plugin-owned command-execution approval (fixed keyboard, not LLM)."""

from __future__ import annotations

import json
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bub.channels.message import ChannelMessage
from bub.hooks.interception import ToolCall
from bub.hooks.interception import ToolCallDecision
from loguru import logger

from .runtime import get_active_channel
from .security import GROUP_PRIVILEGED_ROLES
from .security import QQAccessPolicy
from .security import QQ_CONTEXT_KEY
from .security import QQ_STATE_KEY
from .security import REPLY_TOOL_NAME
from .security import _tool_name_forms
from .security import denied_tool_reason
from .security import parse_id_list
from .session import BoundedDict
from .workspace import tool_escapes_workspace
from .workspace import workspace_from_state

APPROVAL_TTL_SECONDS = 300.0
_PREVIEW_LIMIT = 400
_RESULT_LIMIT = 2000
_MAX_PENDING = 64

_member_roles: dict[tuple[str, str], str] = {}
_always_grants: dict[tuple[str, str, str], None] = {}
_once_grants: dict[tuple[str, str, str], int] = {}


@dataclass
class PendingApproval:
    id: str
    tool: str
    arguments: dict[str, Any]
    session_id: str
    chat_id: str
    requester_id: str
    group_openid: str
    preview: str
    created_at: float
    requester_name: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    workspace: str = ""
    command_line: str = ""


_pending: BoundedDict[str, PendingApproval] = BoundedDict(_MAX_PENDING)


def reset_approval_state() -> None:
    _pending.clear()
    _always_grants.clear()
    _once_grants.clear()
    _member_roles.clear()


def remember_member_role(group_openid: str, member_openid: str, role: str | None) -> None:
    if not group_openid or not member_openid or not role:
        return
    _member_roles[(group_openid, member_openid)] = role


def cached_member_role(group_openid: str, member_openid: str) -> str | None:
    return _member_roles.get((group_openid, member_openid))


def build_approval_keyboard(approval_id: str) -> dict[str, Any]:
    def button(
        button_id: str, label: str, visited: str, data: str, style: int
    ) -> dict[str, Any]:
        return {
            "id": button_id,
            "render_data": {"label": label, "visited_label": visited, "style": style},
            "action": {
                "type": 1,
                "data": data,
                "permission": {"type": 2},
                "click_limit": 1,
            },
            "group_id": "approval",
        }

    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        button(
                            "allow",
                            "✅ 允许一次",
                            "已允许",
                            f"approve:{approval_id}:allow-once",
                            1,
                        ),
                        button(
                            "always",
                            "⭐ 始终允许",
                            "已始终允许",
                            f"approve:{approval_id}:allow-always",
                            1,
                        ),
                        button(
                            "deny",
                            "❌ 拒绝",
                            "已拒绝",
                            f"approve:{approval_id}:deny",
                            0,
                        ),
                    ]
                }
            ]
        }
    }


def parse_approval_button(button_data: str) -> tuple[str, str] | None:
    if not button_data.startswith("approve:"):
        return None
    parts = button_data.split(":")
    if len(parts) != 3:
        return None
    _, approval_id, decision = parts
    if decision not in {"allow-once", "allow-always", "deny"}:
        return None
    return approval_id, decision


def grant_key(session_id: str, sender_id: str, tool: str) -> tuple[str, str, str]:
    return (session_id, sender_id, tool)


def has_always_grant(session_id: str, sender_id: str, tool: str) -> bool:
    return grant_key(session_id, sender_id, tool) in _always_grants


def consume_once_grant(session_id: str, sender_id: str, tool: str) -> bool:
    key = grant_key(session_id, sender_id, tool)
    remaining = _once_grants.get(key, 0)
    if remaining <= 0:
        return False
    if remaining == 1:
        _once_grants.pop(key, None)
    else:
        _once_grants[key] = remaining - 1
    return True


def _approval_reason(
    call: ToolCall,
    state: dict[str, Any],
    qq_state: dict[str, Any],
    config: Any,
) -> str | None:
    """Why this group call needs approval. Privileged senders are not exempt."""

    if REPLY_TOOL_NAME in _tool_name_forms(call.tool):
        return None
    if getattr(config, "workspace_jail", True):
        jail = tool_escapes_workspace(call, workspace_from_state(state))
        if jail is not None:
            return jail
    policy = str(
        getattr(config, "group_tool_policy", "restricted")
        if str(qq_state.get("scope") or "") == "group"
        else getattr(config, "c2c_tool_policy", "open")
    )
    if policy == "locked":
        return None
    return denied_tool_reason(
        tool=call.tool,
        tool_policy=policy,
        extra_denied_patterns=parse_id_list(getattr(config, "denied_tools", "")),
    )


def preview_call(tool: str, arguments: dict[str, Any]) -> str:
    if "cmd" in arguments:
        text = str(arguments.get("cmd") or "")
    elif "path" in arguments:
        text = str(arguments.get("path") or "")
    else:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    text = text.strip() or tool
    if len(text) > _PREVIEW_LIMIT:
        return text[:_PREVIEW_LIMIT] + "…"
    return text


def may_approve(
    config: Any,
    *,
    operator_id: str,
    group_openid: str,
    requester_id: str,
) -> bool:
    if not operator_id:
        return False
    policy = QQAccessPolicy.from_config(config)
    if policy.is_admin_user(operator_id):
        return True
    role = cached_member_role(group_openid, operator_id)
    return (role or "") in GROUP_PRIVILEGED_ROLES


def comma_command_to_call(line: str) -> ToolCall:
    """Map a comma command to the tool Bub would run for it."""

    from bub.builtin import tools as _builtin_tools  # noqa: F401
    from bub.builtin.tools import resolve_tool_name
    from bub.tools import REGISTRY

    body = line[1:].strip() if line.startswith(",") else line.strip()
    if not body:
        return ToolCall(run_id="command", tool="help", arguments={})
    try:
        words = shlex.split(body)
    except ValueError:
        words = body.split()
    name = words[0]
    resolved = resolve_tool_name(name)
    if resolved is None or resolved not in REGISTRY:
        return ToolCall(run_id="command", tool="bash", arguments={"cmd": body})
    arguments: dict[str, Any] = {}
    for token in words[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            arguments[key] = value
    return ToolCall(run_id="command", tool=resolved, arguments=arguments)


async def _enqueue_approval(
    call: ToolCall,
    state: dict[str, Any],
    qq_state: dict[str, Any],
    *,
    deny_reason: str,
    command_line: str = "",
) -> ToolCallDecision:
    session_id = str(qq_state.get("session_id") or state.get("session_id") or "")
    sender_id = str(qq_state.get("sender_id") or "")
    tool = call.tool
    approval_id = uuid.uuid4().hex[:8]
    group_openid = str(qq_state.get("group_openid") or "")
    arguments = dict(call.arguments or {})
    pending = PendingApproval(
        id=approval_id,
        tool=tool,
        arguments=arguments,
        session_id=session_id,
        chat_id=f"group:{group_openid}",
        requester_id=sender_id,
        requester_name=str(qq_state.get("sender_name") or "").strip(),
        group_openid=group_openid,
        preview=(command_line.lstrip(",") if command_line else preview_call(tool, arguments)),
        created_at=time.monotonic(),
        state={
            key: value
            for key, value in state.items()
            if key in {QQ_STATE_KEY, "_runtime_workspace", "session_id"}
        },
        workspace=str(state.get("_runtime_workspace") or ""),
        command_line=command_line,
    )
    _pending[approval_id] = pending
    try:
        await send_approval_message(pending)
    except Exception as exc:
        logger.warning("qq.approval.send_failed id={} error={}", approval_id, exc)
        _pending.pop(approval_id, None)
        return ToolCallDecision.deny(deny_reason)
    logger.info(
        "qq.approval.requested id={} tool={} session_id={} requester={}",
        approval_id,
        tool,
        session_id,
        sender_id,
    )
    return ToolCallDecision.replace(
        f"已提交管理员审批（{approval_id}）。等待群主/管理员点击按钮后再执行。"
    )


async def maybe_request_approval(
    call: ToolCall,
    state: dict[str, Any],
    config: Any,
) -> ToolCallDecision | None:
    """Return a decision when this call should be queued for admin approval."""

    qq_state = state.get(QQ_STATE_KEY)
    if not isinstance(qq_state, dict):
        return None
    if not getattr(config, "exec_approval", True):
        return None
    if str(qq_state.get("scope") or "") != "group":
        return None
    reason = _approval_reason(call, state, qq_state, config)
    if reason is None:
        return None
    return await _enqueue_approval(call, state, qq_state, deny_reason=reason)


async def intercept_group_command(
    message: ChannelMessage,
    *,
    config: Any,
    workspace: Path,
) -> bool:
    """Queue a group comma command for admin approval.

    Returns True when the caller must not forward the command into Bub.
    Jail still decides whether a line is a command; this is the permission
    layer. C2C and ``exec_approval=false`` keep immediate execution.
    """

    if message.kind != "command":
        return False
    if not getattr(config, "exec_approval", True):
        return False
    qq_context = message.context.get(QQ_CONTEXT_KEY)
    if not isinstance(qq_context, dict) or str(qq_context.get("scope") or "") != "group":
        return False
    call = comma_command_to_call(message.content)
    session_id = message.session_id
    sender_id = str(qq_context.get("sender_id") or "")
    if has_always_grant(session_id, sender_id, call.tool):
        return False
    state = {
        QQ_STATE_KEY: {**qq_context, "session_id": session_id},
        "session_id": session_id,
        "_runtime_workspace": str(workspace),
    }
    decision = await _enqueue_approval(
        call,
        state,
        state[QQ_STATE_KEY],
        deny_reason="comma commands require admin approval",
        command_line=message.content,
    )
    return decision is not None


async def send_approval_message(pending: PendingApproval) -> None:
    from .outbound.media import build_outbound_context
    from .outbound.send_flow import is_delivered_send_result

    channel = get_active_channel()
    if channel is None:
        raise RuntimeError("QQ channel is not running")
    body = (
        f"## 命令执行审批\n\n"
        f"- 工具: `{pending.tool}`\n"
        f"- 请求人: {pending.requester_name or pending.requester_id}\n"
        f"- 预览:\n\n> {pending.preview}"
    )
    result = await channel.send_for_result(
        ChannelMessage(
            session_id=pending.session_id,
            channel=channel.name,
            chat_id=pending.chat_id,
            content=body,
            context=build_outbound_context(keyboard=build_approval_keyboard(pending.id)),
        )
    )
    if not is_delivered_send_result(result):
        raise RuntimeError("approval message was not sent")


@dataclass(frozen=True)
class ApprovalClickPlan:
    """PUT ack first, then optionally run the pending call."""

    ack_code: int
    notice: str | None = None
    pending: PendingApproval | None = None
    decision: str | None = None


def begin_approval_click(
    *,
    approval_id: str,
    decision: str,
    operator_id: str,
    config: Any,
) -> ApprovalClickPlan:
    pending = _pending.get(approval_id)
    if pending is None:
        return ApprovalClickPlan(ack_code=1, notice="这条审批不存在或已经处理过。")
    if time.monotonic() - pending.created_at > APPROVAL_TTL_SECONDS:
        _pending.pop(approval_id, None)
        return ApprovalClickPlan(ack_code=1, notice="审批已过期。")
    if not may_approve(
        config,
        operator_id=operator_id,
        group_openid=pending.group_openid,
        requester_id=pending.requester_id,
    ):
        logger.warning(
            "qq.approval.unauthorized id={} operator={}",
            approval_id,
            operator_id,
        )
        return ApprovalClickPlan(
            ack_code=5,
            notice="没有审批权限，仅群主或管理员可操作。",
        )
    _pending.pop(approval_id, None)
    return ApprovalClickPlan(ack_code=0, pending=pending, decision=decision)


async def complete_approval_click(plan: ApprovalClickPlan) -> str | None:
    pending = plan.pending
    decision = plan.decision
    if pending is None or decision is None:
        return plan.notice
    key = grant_key(pending.session_id, pending.requester_id, pending.tool)
    if decision == "deny":
        logger.info("qq.approval.denied id={} tool={}", pending.id, pending.tool)
        return f"已拒绝 `{pending.tool}`。"
    if decision == "allow-always":
        _always_grants[key] = None
        logger.info("qq.approval.always id={} tool={}", pending.id, pending.tool)
    else:
        logger.info("qq.approval.once id={} tool={}", pending.id, pending.tool)
    if pending.command_line:
        output = await _dispatch_approved_command(pending)
    else:
        output = await _execute_pending(pending)
    if decision == "allow-always":
        header = f"已始终允许 `{pending.tool}`。"
    else:
        header = f"已允许一次 `{pending.tool}`。"
    if not output:
        return header
    return f"{header}\n\n{output}"


async def _dispatch_approved_command(pending: PendingApproval) -> str:
    channel = get_active_channel()
    dispatch = getattr(channel, "dispatch_approved_command", None)
    if channel is None or dispatch is None:
        return await _execute_pending(pending)
    try:
        await dispatch(pending)
    except Exception as exc:
        logger.warning("qq.approval.command_dispatch_failed id={} error={}", pending.id, exc)
        return f"执行失败: {exc}"
    return ""


async def resolve_approval_click(
    *,
    approval_id: str,
    decision: str,
    operator_id: str,
    config: Any,
) -> str | None:
    """Test helper: auth + execute without the OpenAPI PUT."""

    plan = begin_approval_click(
        approval_id=approval_id,
        decision=decision,
        operator_id=operator_id,
        config=config,
    )
    return await complete_approval_click(plan)


async def _execute_pending(pending: PendingApproval) -> str:
    try:
        from bub.builtin.tools import resolve_tool_name
        from bub.tape import AsyncTapeStoreAdapter
        from bub.tape import InMemoryTapeStore
        from bub.tape import Tape
        from bub.tape import TapeContext
        from bub.tools import REGISTRY
        from bub.tools import ToolContext
        from bub.tools import ToolExecutor

        resolved = resolve_tool_name(pending.tool) or pending.tool
        tool_obj = REGISTRY.get(resolved)
        if tool_obj is None:
            return f"找不到工具 `{pending.tool}`。"
        workspace = pending.workspace or str(Path.cwd())
        state = dict(pending.state)
        state["_runtime_workspace"] = workspace
        state["session_id"] = pending.session_id
        store = AsyncTapeStoreAdapter(InMemoryTapeStore())
        tape = Tape(
            Path(workspace),
            store,
            TapeContext(state=state),
            _name=pending.session_id,
        )
        context = ToolContext(tape=tape, run_id=pending.id, state=state)
        executor = ToolExecutor(hooks=None)
        execution = await executor.execute_async(
            [(tool_obj, pending.arguments)],
            context=context,
        )
    except Exception as exc:
        logger.warning("qq.approval.execute_failed id={} error={}", pending.id, exc)
        return f"执行失败: {exc}"
    if execution.error is not None:
        return f"执行失败: {execution.error}"
    text = _stringify_result(execution.tool_results[0] if execution.tool_results else "")
    if len(text) > _RESULT_LIMIT:
        return text[:_RESULT_LIMIT] + "…"
    return text or "(no output)"


def _stringify_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


async def send_notice(session_id: str, chat_id: str, text: str) -> None:
    channel = get_active_channel()
    if channel is None:
        return
    await channel.send_for_result(
        ChannelMessage(
            session_id=session_id,
            channel=channel.name,
            chat_id=chat_id,
            content=text,
        )
    )
