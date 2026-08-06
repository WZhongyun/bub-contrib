from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from acp.schema import (
    ClientCapabilities,
    CreateTerminalResponse,
    ReadTextFileResponse,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)
from bub.tools import REGISTRY, ToolContext

from bub_acp_server.client_tools import ACPClientToolRuntime, replace_builtin_tools


class FakeClient:
    def __init__(self) -> None:
        self.read_content = "line two"
        self.read_requests: list[dict[str, object]] = []
        self.write_requests: list[dict[str, object]] = []
        self.create_requests: list[dict[str, object]] = []
        self.wait_requests: list[dict[str, object]] = []
        self.output_requests: list[dict[str, object]] = []
        self.kill_requests: list[dict[str, object]] = []
        self.release_requests: list[dict[str, object]] = []

    async def read_text_file(self, **kwargs: Any) -> ReadTextFileResponse:
        self.read_requests.append(kwargs)
        return ReadTextFileResponse(content=self.read_content)

    async def write_text_file(self, **kwargs: Any) -> None:
        self.write_requests.append(kwargs)

    async def create_terminal(self, **kwargs: Any) -> CreateTerminalResponse:
        self.create_requests.append(kwargs)
        return CreateTerminalResponse(terminal_id="terminal-1")

    async def wait_for_terminal_exit(
        self, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        self.wait_requests.append(kwargs)
        return WaitForTerminalExitResponse(exit_code=0)

    async def terminal_output(self, **kwargs: Any) -> TerminalOutputResponse:
        self.output_requests.append(kwargs)
        return TerminalOutputResponse(
            output="hello\n",
            truncated=False,
            exit_status={"exitCode": 0},
        )

    async def kill_terminal(self, **kwargs: Any) -> None:
        self.kill_requests.append(kwargs)

    async def release_terminal(self, **kwargs: Any) -> None:
        self.release_requests.append(kwargs)


def _runtime(client: FakeClient) -> ACPClientToolRuntime:
    runtime = ACPClientToolRuntime()
    runtime.connect(cast(Any, client))
    runtime.set_capabilities(
        ClientCapabilities(
            fs={"readTextFile": True, "writeTextFile": True}, terminal=True
        )
    )
    return runtime


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        tape=cast(Any, object()),
        state={
            "session_id": "session-1",
            "_runtime_workspace": str(tmp_path),
        },
    )


@pytest.mark.asyncio
async def test_replaces_file_tools_with_acp_client_calls(tmp_path: Path) -> None:
    from bub.builtin import tools as builtin_tools  # noqa: F401

    client = FakeClient()
    context = _context(tmp_path)
    originals = {name: REGISTRY[name] for name in ("fs.read", "fs.write")}

    with replace_builtin_tools(_runtime(client)):
        assert REGISTRY["fs.read"] is not originals["fs.read"]
        assert REGISTRY["fs.write"] is not originals["fs.write"]
        assert REGISTRY["fs.read"].parameters == originals["fs.read"].parameters
        assert REGISTRY["fs.write"].parameters == originals["fs.write"].parameters
        read_result = await REGISTRY["fs.read"].run(
            path="notes.txt", offset=1, limit=3, context=context
        )
        write_result = await REGISTRY["fs.write"].run(
            path="result.txt", content="done", context=context
        )

    assert REGISTRY["fs.read"] is originals["fs.read"]
    assert REGISTRY["fs.write"] is originals["fs.write"]
    assert read_result == "line two"
    assert write_result == f"wrote: {tmp_path / 'result.txt'}"
    assert client.read_requests == [
        {
            "path": str(tmp_path / "notes.txt"),
            "session_id": "session-1",
            "line": 2,
            "limit": 3,
        }
    ]
    assert client.write_requests == [
        {
            "content": "done",
            "path": str(tmp_path / "result.txt"),
            "session_id": "session-1",
        }
    ]


@pytest.mark.asyncio
async def test_replaces_file_edit_with_acp_read_and_write_calls(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.read_content = "before\nold value\nafter\n"
    context = _context(tmp_path)
    original = REGISTRY["fs.edit"]

    with replace_builtin_tools(_runtime(client)):
        assert REGISTRY["fs.edit"] is not original
        assert REGISTRY["fs.edit"].parameters == original.parameters
        result = await REGISTRY["fs.edit"].run(
            path="notes.txt",
            old="old",
            new="new",
            start=1,
            context=context,
        )

    assert REGISTRY["fs.edit"] is original
    assert result == f"edited: {tmp_path / 'notes.txt'}"
    assert client.read_requests == [
        {
            "path": str(tmp_path / "notes.txt"),
            "session_id": "session-1",
            "line": 1,
            "limit": None,
        }
    ]
    assert client.write_requests == [
        {
            "content": "before\nnew value\nafter\n",
            "path": str(tmp_path / "notes.txt"),
            "session_id": "session-1",
        }
    ]


@pytest.mark.asyncio
async def test_edit_preserves_missing_trailing_newline(tmp_path: Path) -> None:
    client = FakeClient()
    client.read_content = "before\nold value\nafter"
    context = _context(tmp_path)

    with replace_builtin_tools(_runtime(client)):
        await REGISTRY["fs.edit"].run(
            path="notes.txt",
            old="old",
            new="new",
            start=1,
            context=context,
        )

    assert client.write_requests == [
        {
            "content": "before\nnew value\nafter",
            "path": str(tmp_path / "notes.txt"),
            "session_id": "session-1",
        }
    ]


@pytest.mark.asyncio
async def test_replaces_bash_with_acp_terminal_calls(tmp_path: Path) -> None:
    from bub.builtin import tools as builtin_tools  # noqa: F401

    client = FakeClient()
    context = _context(tmp_path)
    observed_terminals: list[tuple[str, str]] = []
    original = REGISTRY["bash"]
    runtime = _runtime(client)

    async def observe_terminal(command: str, terminal_id: str) -> None:
        observed_terminals.append((command, terminal_id))

    with replace_builtin_tools(runtime), runtime.observe_terminals(observe_terminal):
        assert REGISTRY["bash"] is not original
        assert REGISTRY["bash"].parameters == original.parameters
        result = await REGISTRY["bash"].run(cmd="pwd", context=context)

    assert REGISTRY["bash"] is original
    assert result == "hello"
    assert observed_terminals == [("pwd", "terminal-1")]
    assert client.create_requests == [
        {
            "command": "bash",
            "args": ["-lc", "pwd"],
            "cwd": str(tmp_path),
            "session_id": "session-1",
        }
    ]
    terminal_request = {"session_id": "session-1", "terminal_id": "terminal-1"}
    assert client.wait_requests == [terminal_request]
    assert client.output_requests == [terminal_request]
    assert client.release_requests == [terminal_request]


@pytest.mark.asyncio
async def test_background_bash_uses_acp_output_and_kill(tmp_path: Path) -> None:
    client = FakeClient()
    context = _context(tmp_path)

    with replace_builtin_tools(_runtime(client)):
        started = await REGISTRY["bash"].run(
            cmd="sleep 10", background=True, context=context
        )
        output = await REGISTRY["bash.output"].run(
            shell_id="terminal-1", context=context
        )
        killed = await REGISTRY["bash.kill"].run(shell_id="terminal-1", context=context)

    terminal_request = {"session_id": "session-1", "terminal_id": "terminal-1"}
    assert started == "started: terminal-1"
    assert "output:\nhello" in output
    assert killed == "id: terminal-1\nstatus: exited\nexit_code: 0"
    assert client.output_requests == [terminal_request, terminal_request]
    assert client.kill_requests == [terminal_request]
    assert client.release_requests == [terminal_request]
