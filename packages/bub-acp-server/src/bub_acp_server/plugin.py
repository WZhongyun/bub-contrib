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
        @app.command("acp", help="Run Bub as an ACP agent.")
        def acp(command: str | None = typer.Argument(None, metavar="[serve]")) -> None:
            if command == "serve":
                typer.echo(
                    "Warning: `bub acp serve` is deprecated; use `bub acp` instead.",
                    err=True,
                )
            elif command is not None:
                raise typer.BadParameter(
                    f"Got unexpected extra argument {command!r}",
                    param_hint="command",
                )
            asyncio.run(run_acp_agent(self.framework))
