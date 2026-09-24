"""Workspace jail: Bub cwd (pwd) is the root for QQ file and shell tools.

The framework already puts ``_runtime_workspace`` on turn state (cwd, or
``bub --workspace``). This module refuses paths and command tokens that
resolve outside that directory. Chat-role privilege does not bypass it.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

from bub.hooks.interception import ToolCall
from bub.turn import TurnState
from loguru import logger

INBOX_NAME = "inbox"
OUTBOX_NAME = "outbox"
MAX_INBOUND_DOWNLOAD_BYTES = 200 * 1024 * 1024

_FS_TOOLS = frozenset(
    {"fs.read", "fs.write", "fs.edit", "fs_read", "fs_write", "fs_edit"}
)
_BASH_TOOLS = frozenset({"bash", "bash.output", "bash.kill", "bash_output", "bash_kill"})
# Names that are not shell. This is not a permission grant: group comma
# commands still go through exec_approval before Bub runs them.
_NON_SHELL_COMMANDS = frozenset(
    {
        "help",
        "quit",
        "model",
        "reasoning_effort",
        "skill",
        "qq.version",
        "qq.send",
        "web.fetch",
        "web_fetch",
        "tape.info",
        "tape.search",
        "tape.reset",
        "tape.handoff",
        "tape.anchors",
        "tape_info",
        "tape_search",
        "tape_reset",
        "tape_handoff",
        "tape_anchors",
    }
)


def workspace_from_state(state: TurnState | dict[str, Any] | None) -> Path:
    raw = state.get("_runtime_workspace") if isinstance(state, dict) else None
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def path_inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return False
    return True


def resolve_in_workspace(
    raw: str,
    workspace: Path,
    *,
    cwd: Path | None = None,
) -> Path | None:
    """Resolve ``raw``; return None when it escapes ``workspace``."""

    text = raw.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    base = cwd if cwd is not None else workspace
    resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
    if not path_inside_workspace(resolved, workspace):
        return None
    return resolved


def tool_escapes_workspace(call: ToolCall, workspace: Path) -> str | None:
    """Denial message when a tool call would leave ``workspace``, else None."""

    name = call.tool.replace("_", ".", 1) if "_" in call.tool else call.tool
    args = call.arguments if isinstance(call.arguments, dict) else {}
    if name in _FS_TOOLS or call.tool in _FS_TOOLS:
        return _path_arg_denied(args.get("path"), workspace)
    if name == "bash" or call.tool == "bash":
        return bash_escapes_workspace(
            cmd=str(args.get("cmd") or ""),
            cwd=args.get("cwd"),
            workspace=workspace,
        )
    if name == "qq.send" or call.tool in {"qq.send", "qq_send"}:
        return _path_arg_denied(args.get("media_path"), workspace)
    return None


def command_escapes_workspace(line: str, workspace: Path) -> str | None:
    """Denial message when a comma-command would leave ``workspace``."""

    body = line[1:].strip() if line.startswith(",") else line.strip()
    if not body:
        return None
    try:
        words = shlex.split(body)
    except ValueError:
        words = body.split()
    if not words:
        return None
    name = words[0]
    parsed = _command_args(words[1:])
    if name in _FS_TOOLS:
        path = parsed.get("path")
        if path is None and parsed["positional"]:
            path = parsed["positional"][0]
        return _path_arg_denied(path, workspace)
    if name in _BASH_TOOLS:
        if name not in {"bash"}:
            return None
        cmd = parsed.get("cmd")
        if cmd is None:
            cmd = " ".join(parsed["positional"])
        return bash_escapes_workspace(
            cmd=str(cmd or ""),
            cwd=parsed.get("cwd"),
            workspace=workspace,
        )
    if name in _NON_SHELL_COMMANDS:
        return None
    return bash_escapes_workspace(cmd=body, cwd=None, workspace=workspace)


def bash_escapes_workspace(
    *, cmd: str, cwd: object, workspace: Path
) -> str | None:
    cwd_path = workspace
    if cwd is not None and str(cwd).strip():
        resolved_cwd = resolve_in_workspace(str(cwd), workspace)
        if resolved_cwd is None:
            return (
                f"cwd '{cwd}' is outside the workspace ({workspace}). "
                "QQ file and shell tools are confined to the process working directory."
            )
        cwd_path = resolved_cwd
    for token in _path_like_tokens(cmd):
        resolved = resolve_in_workspace(token, workspace, cwd=cwd_path)
        if resolved is None:
            return (
                f"path '{token}' is outside the workspace ({workspace}). "
                "QQ file and shell tools are confined to the process working directory."
            )
    return None


def is_unsafe_artifact_workspace(workspace: Path) -> bool:
    """True when writing ``inbox/`` at workspace root would be a bad default.

    ``/`` and ``$HOME`` are writable for some users but are not a dedicated
    work directory. Unwritable roots also fall back.
    """

    root = workspace.expanduser().resolve()
    if root == Path(root.anchor):
        return True
    if root == Path.home().resolve():
        return True
    return not os.access(root, os.W_OK)


def artifact_root(workspace: Path) -> Path:
    """Where bub-qq writes inbound/generated files.

    Dedicated workspaces (the recommended ``cd`` target) get ``inbox/`` and
    ``outbox/`` at the workspace root. ``/``, ``$HOME``, and unwritable
    directories fall back to ``<bub home>/qq`` so files never scatter at
    the filesystem root.
    """

    root = workspace.expanduser().resolve()
    if not is_unsafe_artifact_workspace(root):
        return root
    import bub

    fallback = (bub.home / "qq").expanduser().resolve()
    logger.info(
        "qq.workspace.artifact_fallback workspace={} dest={}",
        root,
        fallback,
    )
    return fallback


def inbox_dir(workspace: Path, message_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", message_id)[:64] or "message"
    return artifact_root(workspace) / INBOX_NAME / safe_id


def outbox_dir(workspace: Path) -> Path:
    return artifact_root(workspace) / OUTBOX_NAME


def safe_filename(name: str | None, url: str | None, index: int) -> str:
    raw = (name or "").strip() or Path(unquote(urlparse(url or "").path)).name
    raw = Path(raw).name.strip()
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    return raw or f"file-{index}"


def _path_arg_denied(raw: object, workspace: Path) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if resolve_in_workspace(text, workspace) is None:
        return (
            f"path '{text}' is outside the workspace ({workspace}). "
            "QQ file and shell tools are confined to the process working directory."
        )
    return None


def _path_like_tokens(cmd: str) -> list[str]:
    if not cmd.strip():
        return []
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    tokens: list[str] = []
    for index, word in enumerate(words):
        if index == 0:
            continue
        if word.startswith("-"):
            continue
        lowered = word.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            continue
        if word.startswith("/") or word.startswith("~") or "/" in word or word in {".", ".."}:
            tokens.append(word)
    return tokens


def _command_args(tokens: list[str]) -> dict[str, Any]:
    positional: list[str] = []
    kwargs: dict[str, Any] = {"positional": positional}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            kwargs[key] = value
        else:
            positional.append(token)
    return kwargs
