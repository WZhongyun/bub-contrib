from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bub.channels.message import ChannelMessage
from bub.hooks.interception import ToolCall

from bub_qq import plugin
from bub_qq.config import QQConfig
from bub_qq.inbound.c2c import build_c2c_channel_message
from bub_qq.inbound.persist import persist_inbound_attachments
from bub_qq.protocol.models import QQAttachment
from bub_qq.protocol.models import QQC2CMessage
from bub_qq.workspace import artifact_root
from bub_qq.workspace import bash_escapes_workspace
from bub_qq.workspace import command_escapes_workspace
from bub_qq.workspace import inbox_dir
from bub_qq.workspace import is_unsafe_artifact_workspace
from bub_qq.workspace import tool_escapes_workspace
from bub_qq.workspace import workspace_from_state


def test_artifact_root_uses_workspace_for_dedicated_dir(tmp_path: Path) -> None:
    assert is_unsafe_artifact_workspace(tmp_path) is False
    assert artifact_root(tmp_path) == tmp_path.resolve()
    assert inbox_dir(tmp_path, "msg-1") == tmp_path.resolve() / "inbox" / "msg-1"


def test_artifact_root_falls_back_for_home_and_slash(tmp_path: Path, monkeypatch) -> None:
    import bub

    home = tmp_path / "home"
    home.mkdir()
    bub_home = tmp_path / "bub-home"
    bub_home.mkdir()
    monkeypatch.setattr("bub_qq.workspace.Path.home", lambda: home)
    monkeypatch.setattr(bub, "home", bub_home)

    assert is_unsafe_artifact_workspace(home) is True
    assert artifact_root(home) == (bub_home / "qq").resolve()
    assert artifact_root(Path("/")) == (bub_home / "qq").resolve()
    assert inbox_dir(Path("/"), "msg-1") == (bub_home / "qq" / "inbox" / "msg-1")


def test_workspace_from_state_uses_runtime_then_cwd(tmp_path: Path, monkeypatch) -> None:
    assert workspace_from_state({"_runtime_workspace": str(tmp_path)}) == tmp_path.resolve()
    monkeypatch.chdir(tmp_path)
    assert workspace_from_state({}) == tmp_path.resolve()


def test_bash_and_fs_tokens_inside_workspace_are_allowed(tmp_path: Path) -> None:
    inside = tmp_path / "notes.txt"
    inside.write_text("ok", encoding="utf-8")
    assert (
        bash_escapes_workspace(cmd="cat notes.txt", cwd=None, workspace=tmp_path)
        is None
    )
    assert (
        tool_escapes_workspace(
            ToolCall(run_id="r", tool="fs.read", arguments={"path": "notes.txt"}),
            tmp_path,
        )
        is None
    )


def test_bash_cwd_and_absolute_args_outside_workspace_are_denied(tmp_path: Path) -> None:
    assert (
        bash_escapes_workspace(cmd="ls", cwd="/tmp", workspace=tmp_path) is not None
    )
    assert (
        bash_escapes_workspace(cmd="cat /etc/passwd", cwd=None, workspace=tmp_path)
        is not None
    )
    assert (
        bash_escapes_workspace(cmd="python notes.py", cwd=None, workspace=tmp_path)
        is None
    )


def test_command_line_unknown_shell_is_jailed(tmp_path: Path) -> None:
    assert command_escapes_workspace(",cat /etc/passwd", tmp_path) is not None
    assert command_escapes_workspace(",qq.version", tmp_path) is None
    assert command_escapes_workspace(",fs.read path=notes.txt", tmp_path) is None
    assert command_escapes_workspace(",fs.read path=/etc/passwd", tmp_path) is not None


def test_privileged_tool_policy_still_hits_workspace_jail(
    monkeypatch, tmp_path: Path
) -> None:
    import bub

    monkeypatch.setattr(
        bub,
        "ensure_config",
        lambda cls: QQConfig.model_construct(
            admin_users="admin-1",
            group_tool_policy="restricted",
            exec_approval=False,
            workspace_jail=True,
        ),
    )
    state = {
        "qq": {
            "scope": "group",
            "sender_id": "admin-1",
            "sender_role": "member",
            "session_id": "qq:group:g",
        },
        "_runtime_workspace": str(tmp_path),
    }
    allowed = asyncio.run(
        plugin.before_tool_call(
            ToolCall(run_id="r", tool="fs.read", arguments={"path": "notes.txt"}),
            state,
        )
    )
    denied = asyncio.run(
        plugin.before_tool_call(
            ToolCall(run_id="r", tool="fs.read", arguments={"path": "/etc/passwd"}),
            state,
        )
    )
    assert allowed is None
    assert denied is not None
    assert denied.action == "deny"
    assert "outside the workspace" in (denied.message or "")


def test_c2c_command_outside_workspace_is_not_kind_command(tmp_path: Path) -> None:
    message = QQC2CMessage(
        message_id="m1",
        user_openid="admin",
        content=",cat /etc/passwd",
        timestamp=None,
        attachments=(),
        event_id=None,
        sequence=None,
    )
    built = build_c2c_channel_message(
        "qq", message, allow_command=True, workspace=tmp_path
    )
    assert built.kind == "normal"
    payload = json.loads(built.content)
    assert payload["type"] == "command_blocked"


def test_c2c_command_inside_workspace_stays_command(tmp_path: Path) -> None:
    message = QQC2CMessage(
        message_id="m1",
        user_openid="admin",
        content=",qq.version",
        timestamp=None,
        attachments=(),
        event_id=None,
        sequence=None,
    )
    built = build_c2c_channel_message(
        "qq", message, allow_command=True, workspace=tmp_path
    )
    assert built.kind == "command"
    assert built.content == ",qq.version"


def test_persist_inbound_attachments_writes_under_inbox(
    tmp_path: Path, monkeypatch
) -> None:
    async def _run() -> None:
        source = tmp_path / "remote.png"
        source.write_bytes(b"png-bytes")

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            @property
            def content(self) -> object:
                return self

            async def iter_chunked(self, size: int):
                del size
                yield b"png-bytes"

            async def __aenter__(self) -> _Resp:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

        class _Session:
            def get(self, url: str) -> _Resp:
                assert url == "https://example.com/a.png"
                return _Resp()

            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

        monkeypatch.setattr(
            "bub_qq.inbound.persist.aiohttp.ClientSession", lambda **kwargs: _Session()
        )
        content = json.dumps(
            {
                "message": "",
                "attachments": [
                    {"url": "https://example.com/a.png", "filename": "a.png"}
                ],
            }
        )
        attachment = QQAttachment(
            content_type="image/png",
            filename="a.png",
            height=1,
            width=1,
            size=9,
            url="https://example.com/a.png",
            voice_wav_url=None,
            asr_refer_text=None,
        )
        updated = await persist_inbound_attachments(
            content,
            (attachment,),
            workspace=tmp_path,
            message_id="msg-1",
        )
        payload = json.loads(updated)
        local = Path(payload["attachments"][0]["local_path"])
        assert local.is_file()
        assert local.read_bytes() == b"png-bytes"
        assert local.is_relative_to(tmp_path / "inbox")

    asyncio.run(_run())
