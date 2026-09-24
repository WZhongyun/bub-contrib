"""Guard: the single checkpoint every QQ-initiated action passes through.

Model tool calls (``before_tool_call``), comma commands (intercepted by the
channel before Bub sees them, because Bub runs commands without hooks) and
approved executions all call :func:`evaluate`. It decides from two things
only: who is asking (:class:`Requester`, trusted when listed as an admin)
and which resource the call touches (:func:`classify`).

Trust never comes from QQ group roles: the deployer is not necessarily the
group owner, so owners and admins of a group are ordinary members unless
the deployer lists them in ``admin_users`` or they register as admins.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bub.hooks.interception import ToolCall

from .security import REPLY_TOOL_NAME
from .security import denied_tool_reason
from .security import parse_id_list
from .workspace import resolve_in_workspace
from .workspace import tool_protected_reason

if TYPE_CHECKING:
    from .config import QQConfig

type Resource = Literal[
    "reply", "shell", "shell_session", "fs_read", "fs_write", "fetch", "other"
]
type Action = Literal["allow", "deny", "approval"]

_SHELL_TOOLS = frozenset({"bash"})
_SHELL_SESSION_TOOLS = frozenset({"bash.output", "bash.kill"})
_FS_READ_TOOLS = frozenset({"fs.read"})
_FS_WRITE_TOOLS = frozenset({"fs.write", "fs.edit"})
_FETCH_TOOLS = frozenset({"web.fetch"})

_registered_admins: frozenset[str] = frozenset()


def set_registered_admins(identities: Iterable[str]) -> None:
    """Admins registered at runtime (pairing), merged with ``admin_users``."""

    global _registered_admins
    _registered_admins = frozenset(identities)


def registered_admins() -> frozenset[str]:
    return _registered_admins


@dataclass(frozen=True)
class Requester:
    """Who is asking, in the scope they asked from.

    The same person has a different openid in C2C (``user_openid``) and in
    each group (``member_openid``), so identities are scoped.
    """

    scope: Literal["group", "c2c"]
    sender_id: str
    group_openid: str = ""

    @property
    def identity(self) -> str:
        if self.scope == "group":
            return f"group:{self.group_openid}:{self.sender_id}"
        return f"c2c:{self.sender_id}"

    @classmethod
    def from_state(cls, qq_state: dict[str, Any]) -> Requester:
        scope = "group" if str(qq_state.get("scope") or "") == "group" else "c2c"
        return cls(
            scope=scope,
            sender_id=str(qq_state.get("sender_id") or ""),
            group_openid=str(qq_state.get("group_openid") or ""),
        )


def admin_entries(config: QQConfig) -> frozenset[str]:
    return parse_id_list(config.admin_users) | _registered_admins


def is_admin(config: QQConfig, requester: Requester) -> bool:
    """Scoped identities match exactly; bare openids (legacy) match any scope."""

    if not requester.sender_id:
        return False
    entries = admin_entries(config)
    return requester.identity in entries or requester.sender_id in entries


def group_admin_member_ids(config: QQConfig, group_openid: str) -> list[str]:
    """``member_openid`` values of admins known for one group."""

    prefix = f"group:{group_openid}:"
    ids: list[str] = []
    for entry in sorted(admin_entries(config)):
        if entry.startswith(prefix):
            ids.append(entry.removeprefix(prefix))
        elif ":" not in entry:
            ids.append(entry)
    return ids


@dataclass(frozen=True)
class Decision:
    action: Action
    resource: Resource
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


def canonical_tool(tool: str) -> str:
    """Registry name for a tool (``fs_read`` → ``fs.read``)."""

    return tool.replace("_", ".", 1) if "." not in tool and "_" in tool else tool


def classify(call: ToolCall) -> Resource:
    tool = canonical_tool(call.tool)
    if tool == REPLY_TOOL_NAME:
        return "reply"
    if tool in _SHELL_TOOLS:
        return "shell"
    if tool in _SHELL_SESSION_TOOLS:
        return "shell_session"
    if tool in _FS_READ_TOOLS:
        return "fs_read"
    if tool in _FS_WRITE_TOOLS:
        return "fs_write"
    if tool in _FETCH_TOOLS:
        # Allowed by policy like other tools, but the plugin performs the
        # fetch itself so it can only reach public addresses.
        return "fetch"
    return "other"


def evaluate(
    call: ToolCall,
    requester: Requester,
    *,
    config: QQConfig,
    workspace: Path,
    protected: Iterable[Path] = (),
    approved: bool = False,
) -> Decision:
    """Decide one call. ``approved`` means a valid approval token was used."""

    resource = classify(call)
    protected_reason = tool_protected_reason(call, workspace, extra=tuple(protected))
    if protected_reason is not None:
        return Decision("deny", resource, protected_reason)
    if resource == "reply":
        return Decision("allow", resource)

    admin = is_admin(config, requester)
    if requester.scope == "c2c" and not admin and config.c2c_access == "admin_users":
        return Decision(
            "deny",
            resource,
            "Tools are only available to admins in private chat.",
        )

    tool_policy = (
        config.group_tool_policy if requester.scope == "group" else config.c2c_tool_policy
    )
    if tool_policy == "locked":
        return Decision("deny", resource, "Tool calls are disabled in this chat.")

    if resource == "shell":
        if not admin:
            return Decision("deny", resource, "Shell commands are admin-only.")
        if config.group_shell == "deny":
            return Decision("deny", resource, "Shell commands are disabled (group_shell=deny).")
        if approved:
            return Decision("allow", resource)
        return Decision("approval", resource, "Shell commands need admin approval.")

    if resource == "shell_session":
        # Reading or stopping a shell that an approved command started.
        if not admin:
            return Decision("deny", resource, "Shell commands are admin-only.")
        return Decision("allow", resource)

    if resource in {"fs_read", "fs_write"}:
        if resource == "fs_write" and not admin:
            return Decision("deny", resource, "Writing files is admin-only.")
        raw = (call.arguments or {}).get("path") if isinstance(call.arguments, dict) else None
        if raw is not None and str(raw).strip():
            if resolve_in_workspace(str(raw), workspace) is None:
                return Decision(
                    "deny",
                    resource,
                    f"path '{raw}' is outside the workspace ({workspace}).",
                )
        return Decision("allow", resource)

    reason = denied_tool_reason(
        tool=call.tool,
        tool_policy=tool_policy,
        extra_denied_patterns=parse_id_list(config.denied_tools),
    )
    if reason is not None:
        return Decision("deny", resource, reason)
    return Decision("allow", resource)
