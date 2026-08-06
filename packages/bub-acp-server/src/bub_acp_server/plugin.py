from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer
from bub import hookimpl

from bub_acp_server.agent import BubACPAgent, run_acp_agent

if TYPE_CHECKING:
    from bub.framework import BubFramework

__all__ = ["ACPServerPlugin", "BubACPAgent", "run_acp_agent"]


class ACPServerPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        acp_app = typer.Typer(
            name="acp", help="Run Bub as an ACP agent.", add_completion=False
        )

        @acp_app.command("serve")
        def serve() -> None:
            asyncio.run(run_acp_agent(self.framework))

        app.add_typer(acp_app, name="acp")
