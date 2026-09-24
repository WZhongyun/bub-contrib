"""Native tools and ops comma commands for the QQ channel.

``qq.send`` is the reply tool for ``reply_mode="tool"``: direct model
output is routed to the "null" channel, so calling the tool is the only
way a reply reaches the chat and *not* calling it is how the model stays
silent. It reuses the channel's send services, so passive
``msg_id``/``msg_seq`` targeting, dedupe and the active-message fallback
all behave exactly as in direct mode.

Tools registered with ``agent_use=False`` are ops comma commands: they
never appear in the model's tool list and can only be requested as
``,name`` by senders who pass the comma-command gate (group
owners/admins or ``admin_users``). In groups with ``exec_approval``,
that request still waits for an admin keyboard tap.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

import bub
from bub.channels.message import ChannelMessage
from bub.tools import ToolContext, tool

from .config import QQConfig
from .outbound.media import MediaSpec
from .outbound.media import build_outbound_context
from .outbound.media import infer_file_type
from .outbound.media import media_spec_from_args
from .outbound.media import parse_at_user_ids
from .outbound.media import resolve_media_path
from .runtime import get_active_channel
from .security import QQ_STATE_KEY, REPLY_TOOL_NAME


@tool(name=REPLY_TOOL_NAME, context=True)
async def qq_send(
    content: str = "",
    media_url: str | None = None,
    media_path: str | None = None,
    file_type: int | None = None,
    at_user_ids: str | list[str] | None = None,
    *,
    context: ToolContext,
) -> str:
    """Send a message to the current QQ chat (group or private).

    Pass the message text, or media_url / workspace media_path for a
    native image/video/voice/file (msg_type=7). media_url is downloaded
    by this plugin and uploaded as a local file. In group chats, pass
    at_user_ids with member openids (from inbound sender_id) to @ them
    with a native mention chip. Reply targeting (msg_id/msg_seq) is
    handled by the channel. Call once per message you want delivered.
    To stay silent this turn, do not call this tool.
    """

    config = bub.ensure_config(QQConfig)
    if config.reply_mode != "tool":
        return (
            "Not sent: qq.send is disabled (qq.reply_mode is 'direct')."
            " Write your reply as plain text instead."
        )
    qq_state = context.state.get(QQ_STATE_KEY)
    if not isinstance(qq_state, dict):
        return "Not sent: qq.send is only available inside QQ channel sessions."
    channel = get_active_channel()
    if channel is None:
        return "Not sent: the QQ channel is not running in this process."

    if media_url and media_path:
        return "Not sent: pass media_url or media_path, not both."
    media, media_error = media_spec_from_args(media_url, file_type)
    if media_error is not None:
        return media_error
    if media_path:
        workspace = context.state.get("_runtime_workspace")
        workspace_text = (
            str(workspace) if isinstance(workspace, str | Path) else None
        )
        resolved, path_error = resolve_media_path(media_path, workspace_text)
        if path_error is not None:
            return path_error
        if resolved is None:
            return "Not sent: media_path is empty."
        resolved_type = file_type if file_type is not None else infer_file_type(str(resolved))
        media = MediaSpec(
            file_type=resolved_type,
            file_name=resolved.name,
            local_path=str(resolved),
        )
    mentions, mention_error = parse_at_user_ids(at_user_ids)
    if mention_error is not None:
        return mention_error
    if not (content or "").strip() and media is None and not mentions:
        return "Not sent: provide content, media_url, or at_user_ids."

    session_id = str(qq_state.get("session_id") or "")
    if str(qq_state.get("scope") or "") == "group":
        chat_id = f"group:{qq_state.get('group_openid') or ''}"
    else:
        chat_id = f"c2c:{qq_state.get('sender_id') or ''}"

    result = await channel.send_for_result(
        ChannelMessage(
            session_id=session_id,
            channel=channel.name,
            chat_id=chat_id,
            content=content,
            context=build_outbound_context(media=media, at_user_ids=mentions),
        )
    )
    return format_send_result(result)


def format_send_result(result: dict[str, object] | None) -> str:
    if result is None:
        return (
            "Not sent: the QQ channel skipped this send"
            " (empty content, closed reply window, or no remaining passive replies;"
            " see gateway logs). Do not retry with identical content."
        )
    status = result.get("status")
    if status == "pending_audit":
        return "Accepted: QQ queued the message for manual review before delivery."
    if status == "already_sent":
        return "Skipped: identical content was already sent for this reply window."
    if status == "failed":
        return _format_failed_send(result)
    return "Sent."


def _format_failed_send(result: dict[str, object]) -> str:
    code = result.get("error_code")
    error = str(result.get("error") or "platform error").strip()
    if "failed to download media_url" in error:
        return (
            f"Not sent: {error}."
            " The plugin downloads media_url itself; try another URL or a workspace"
            " media_path. Do not retry the same media_url, and do not bash/curl it."
        )
    if code == 850027:
        return (
            "Not sent: QQ timed out uploading rich media (code=850027 富媒体文件上传超时)."
            " If you passed media_url, the plugin already downloaded it locally;"
            " retry with media_path if the file is in the workspace."
            " Do not retry the same failed payload."
        )
    if code is None:
        return f"Not sent: {error}. Do not retry with identical content."
    return (
        f"Not sent: QQ OpenAPI error code={code} {error}."
        " Do not retry with identical content."
    )


@tool(name="qq.version", agent_use=False)
def qq_version() -> str:
    """Show the installed bub-qq plugin version (ops comma command)."""

    try:
        return f"bub-qq {package_version('bub-qq')}"
    except PackageNotFoundError:
        return "bub-qq (version unknown: package metadata not found)"
