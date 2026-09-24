from __future__ import annotations

from typing import Literal

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict

type ToolPolicy = Literal["open", "restricted", "locked"]
type ReplyMode = Literal["direct", "tool"]
type GroupShell = Literal["deny", "approval"]
type ShellSandbox = Literal["none", "external"]
type C2CAccess = Literal["admin_users", "allow_users"]


@bub.config(name="qq")
class QQConfig(bub.Settings):
    """QQ Open Platform adapter config."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_QQ_",
        env_file=".env",
        extra="ignore",
    )

    appid: str = ""
    secret: str = ""
    token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    openapi_base_url: str = "https://api.bot.qq.com"
    timeout_seconds: float = 30.0
    token_refresh_skew_seconds: int = 60
    receive_mode: str = Field(
        default="",
        description="QQ inbound transport mode. Must be set to 'webhook' or 'websocket' before gateway start.",
    )
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_path: str = "/qq/webhook"
    webhook_callback_timeout_seconds: float = 15.0
    verify_signature: bool = True
    webhook_signature_timestamp_tolerance_seconds: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Reject webhook requests whose signature timestamp deviates from local"
            " time by more than this many seconds. 0 disables the freshness check."
        ),
    )
    inbound_dedupe_size: int = Field(default=1024, ge=1)
    session_state_size: int = Field(
        default=1024,
        ge=1,
        description="Max sessions/send records kept in memory for passive replies.",
    )
    passive_reply_window_seconds: float = Field(
        default=3600.0,
        gt=0,
        description="How long after an inbound message passive replies are attempted.",
    )
    active_messages: bool = Field(
        default=False,
        description=(
            "Send proactive group messages (no msg_id) when a passive reply"
            " is impossible. Requires the group admin to allow proactive"
            " messages in the QQ client; consumes platform quota."
        ),
    )
    reply_mode: ReplyMode = Field(
        default="tool",
        description=(
            "How model output reaches QQ. 'tool' (default) disables direct"
            " forwarding and exposes the qq.send tool; staying silent means"
            " not calling it. 'direct' forwards the model's final text as"
            " the reply (output exactly <no_reply/> to stay silent)."
        ),
    )
    passive_replies_per_msg_id: int = Field(
        default=4,
        ge=1,
        description=(
            "Local cap of passive replies per inbound msg_id, aligned with"
            " the platform limit; beyond it the send falls back to an active"
            " message (when enabled) or is skipped."
        ),
    )
    state_file: str = Field(
        default="",
        description=(
            "Path of the JSON file persisting platform switches (active"
            "-message opt-ins, group claw_cfg). Empty uses"
            " <bub home>/qq/state.json."
        ),
    )
    websocket_intents: int = 1 << 25
    websocket_use_shard_gateway: bool = False
    websocket_reconnect_delay_seconds: float = 5.0
    websocket_reconnect_max_delay_seconds: float = 300.0
    websocket_max_identify_rejections: int = 5
    admin_users: str = Field(
        default="",
        description=(
            "Comma-separated admins, the only trusted senders. Entries are"
            " scoped identities ('c2c:<user_openid>',"
            " 'group:<group_openid>:<member_openid>') or bare openids that"
            " match in any scope. QQ group owners/admins are not trusted"
            " unless listed here or registered with ,qq.claim. Comma commands"
            " from anyone else are treated as plain text."
        ),
    )
    allow_users: str = Field(
        default="",
        description=(
            "Comma-separated C2C user openid allowlist. When set, C2C"
            " messages from anyone else are dropped. Empty allows everyone."
        ),
    )
    allow_groups: str = Field(
        default="",
        description=(
            "Comma-separated group openid allowlist. When set, messages from"
            " other groups are dropped. Empty allows every group."
        ),
    )
    group_shell: GroupShell = Field(
        default="approval",
        description=(
            "Shell (bash) access for admins: 'approval' asks an admin to"
            " confirm every command on a keyboard card (no always-allow),"
            " 'deny' refuses shell entirely. Non-admins never get a shell."
            " Applies in groups and in C2C."
        ),
    )
    shell_sandbox: ShellSandbox = Field(
        default="none",
        description=(
            "Declare whether bash runs inside an external sandbox (container,"
            " bwrap, ...). The plugin cannot verify this and does not isolate"
            " commands itself; 'none' logs a warning when shell is enabled."
        ),
    )
    c2c_access: C2CAccess = Field(
        default="admin_users",
        description=(
            "Who may use tools in C2C: 'admin_users' (admins only) or"
            " 'allow_users' (everyone in the non-empty allow_users list)."
            " Replies via qq.send always work."
        ),
    )
    download_allow_hosts: str = Field(
        default="",
        description=(
            "Comma-separated hostnames qq.send media_url may fetch even when"
            " they resolve to a private address (e.g. an intranet image"
            " host). Every other host must be a public internet address."
        ),
    )
    exec_approval: bool | None = Field(
        default=None,
        description="Removed in 0.3.0; replaced by group_shell. Ignored with a warning.",
    )
    workspace_jail: bool | None = Field(
        default=None,
        description=(
            "Removed in 0.3.0; replaced by the Guard allowlists and"
            " shell_sandbox. Ignored with a warning."
        ),
    )
    group_tool_policy: ToolPolicy = Field(
        default="restricted",
        description=(
            "Policy for tools other than shell and files in group sessions:"
            " 'open' allows them, 'restricted' denies subagent and"
            " denied_tools, 'locked' denies all of them. Applies to admins"
            " too; shell and file access follow group_shell and the Guard."
        ),
    )
    c2c_tool_policy: ToolPolicy = Field(
        default="open",
        description="Tool policy for C2C sessions; same values as group_tool_policy.",
    )
    denied_tools: str = Field(
        default="",
        description=(
            "Extra comma-separated tool-name glob patterns denied under the"
            " 'restricted' policy, e.g. 'web.fetch,tape.*'."
        ),
    )
    llm_rate_limit_per_minute: int = Field(
        default=0,
        ge=0,
        description=(
            "Max LLM calls per sender per session per minute; exceeding calls"
            " are short-circuited with llm_rate_limit_notice. 0 disables."
        ),
    )
    llm_rate_limit_notice: str = Field(
        default="请求过于频繁，请稍后再试。",
        description="Reply text used when a sender hits the LLM rate limit.",
    )
