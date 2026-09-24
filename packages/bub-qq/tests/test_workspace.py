from __future__ import annotations

from pathlib import Path


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
