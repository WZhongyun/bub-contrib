from __future__ import annotations

import asyncio

from bub.tools import REGISTRY

from bub_qq import tools


def _command_only(tool) -> bool:
    # Bub 0.5.0 has ``agent_use``; later Bub has ``exposure`` (bubbuild/bub#347).
    if hasattr(tool, "exposure"):
        return tool.exposure == "command"
    return tool.agent_use is False


def test_qq_version_is_registered_as_command_only() -> None:
    assert _command_only(REGISTRY["qq.version"])
    assert not _command_only(REGISTRY["qq.send"])


def test_qq_version_reports_package_version() -> None:
    result = asyncio.run(tools.qq_version.run())

    assert result.startswith("bub-qq ")
