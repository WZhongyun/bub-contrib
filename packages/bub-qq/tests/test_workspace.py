from __future__ import annotations

import asyncio
import json
from pathlib import Path


from bub_qq.inbound.persist import persist_inbound_attachments
from bub_qq.protocol.models import QQAttachment
from bub_qq.workspace import artifact_root
from bub_qq.workspace import inbox_dir
from bub_qq.workspace import is_unsafe_artifact_workspace
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
