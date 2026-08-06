from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.schema import (
    ClientCapabilities,
    CreateTerminalResponse,
    ReadTextFileResponse,
    TerminalOutputResponse,
    TextContentBlock,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)


class E2EClient:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.read_requests: list[dict[str, object]] = []
        self.write_requests: list[dict[str, object]] = []
        self.create_requests: list[dict[str, object]] = []
        self.session_updates: list[tuple[str, object]] = []

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        del kwargs
        self.read_requests.append(
            {"path": path, "session_id": session_id, "line": line, "limit": limit}
        )
        lines = self.files[path].splitlines()
        start = max(0, (line or 1) - 1)
        end = len(lines) if limit is None else start + limit
        return ReadTextFileResponse(content="\n".join(lines[start:end]))

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **kwargs: Any,
    ) -> WriteTextFileResponse:
        del kwargs
        self.write_requests.append(
            {"content": content, "path": path, "session_id": session_id}
        )
        self.files[path] = content
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        del kwargs
        self.create_requests.append(
            {
                "command": command,
                "args": args,
                "cwd": cwd,
                "session_id": session_id,
            }
        )
        return CreateTerminalResponse(terminal_id="terminal-e2e")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        del session_id, terminal_id, kwargs
        return WaitForTerminalExitResponse(exit_code=0)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        del session_id, terminal_id, kwargs
        return TerminalOutputResponse(
            output="e2e-command",
            truncated=False,
            exit_status={"exitCode": 0},
        )

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> None:
        del session_id, terminal_id, kwargs

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        del kwargs
        self.session_updates.append((session_id, update))


@pytest.mark.asyncio
async def test_acp_prompt_executes_bub_tools_through_client(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target_path = workspace / "target.txt"
    created_path = workspace / "created.txt"
    client = E2EClient(
        {str(target_path): "before\nold value\nafter\n"}
    )
    server_script = Path(__file__).parent / "fixtures" / "acp_tool_server.py"

    async with spawn_agent_process(
        client,
        sys.executable,
        str(server_script),
        env={
            "BUB_ACP_E2E_WORKSPACE": str(workspace),
            "BUB_HOME": str(tmp_path / ".bub"),
        },
        use_unstable_protocol=True,
    ) as (connection, process):
        async with asyncio.timeout(10):
            initialized = await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs={"readTextFile": True, "writeTextFile": True},
                    terminal=True,
                ),
            )
            session = await connection.new_session(cwd=str(workspace))
            response = await connection.prompt(
                [TextContentBlock(type="text", text="exercise client tools")],
                session_id=session.session_id,
            )

        assert process.returncode is None
        assert initialized.protocol_version == PROTOCOL_VERSION
        assert response.stop_reason == "end_turn"

    assert [request["path"] for request in client.read_requests] == [
        str(target_path),
        str(target_path),
    ]
    assert client.files[str(created_path)] == "created by ACP"
    assert client.files[str(target_path)] == "before\nnew value\nafter"
    assert client.create_requests == [
        {
            "command": "bash",
            "args": ["-lc", "printf e2e-command"],
            "cwd": str(workspace),
            "session_id": session.session_id,
        }
    ]

    text_updates = [
        update.content.text
        for _, update in client.session_updates
        if update.session_update == "agent_message_chunk"
    ]
    payload = json.loads("".join(text_updates))
    assert payload["read"] == "old value"
    assert payload["bash"] == "e2e-command"
    assert payload["write"] == f"wrote: {created_path}"
    assert payload["edit"] == f"edited: {target_path}"
