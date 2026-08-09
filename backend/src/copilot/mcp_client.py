"""Sync wrapper over the async MCP stdio client, for use inside the agent graph."""
import asyncio
import json
import sys
import threading

from copilot.config import REPO_ROOT


class McpError(RuntimeError):
    pass


def _parse_tool_result(result) -> tuple[list, list]:
    # NOTE: the installed `mcp` package (2.0.0) renamed the CallToolResult
    # attributes from the camelCase `isError`/`structuredContent` (still used
    # as the wire-protocol JSON aliases, and by hand-built test doubles like
    # this module's own unit tests) to snake_case `is_error`/`structured_content`
    # as the actual Python attribute names. Check both so this works against
    # real SDK objects and against simple camelCase fakes.
    is_error = getattr(result, "isError", None)
    if is_error is None:
        is_error = getattr(result, "is_error", False)
    if is_error:
        text = result.content[0].text if result.content else "MCP tool error"
        raise McpError(text)
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        payload = getattr(result, "structured_content", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    if "result" in payload and "columns" not in payload:  # FastMCP wraps plain returns
        payload = payload["result"]
    return list(payload["columns"]), [list(r) for r in payload["rows"]]


class McpExecutor:
    """Owns a background event loop + stdio MCP session. One per process.

    The MCP session's async context managers (stdio_client, ClientSession) use
    anyio task groups internally, whose cancel scopes must be entered and exited
    from the *same* asyncio Task. Scheduling __aenter__ and __aexit__ as two
    separate coroutines via run_coroutine_threadsafe puts them in two different
    Tasks and raises "Attempted to exit cancel scope in a different task than
    it was entered in". So instead the whole session lifetime -- enter, idle
    until told to stop, exit -- runs as one coroutine/Task (`_main`), and
    `run_query`/`close` talk to it via run_coroutine_threadsafe + an asyncio.Event.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._session = None
        self._stop_event = None
        self._start_error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=60):
            raise McpError("timed out starting MCP server subprocess")
        if self._start_error is not None:
            raise self._start_error

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        import os

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stop_event = asyncio.Event()
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(REPO_ROOT / "mcp_server" / "server.py")],
            # stdio_client's default env is a safe allowlist (PATH, HOME, ...)
            # that drops app-specific vars like COPILOT_FAKE_SNOWFLAKE -- pass
            # the full parent environment through so the subprocess sees it.
            env=dict(os.environ))
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop_event.wait()
        except Exception as exc:  # noqa: BLE001 — surfaced to __init__ via _start_error
            self._start_error = McpError(f"failed to start MCP session: {exc}")
            self._ready.set()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

    def run_query(self, sql: str) -> tuple[list, list]:
        async def call():
            return await self._session.call_tool("run_query", {"sql": sql})

        return _parse_tool_result(self._run(call()))

    def close(self):
        if self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=10)
        # `_main` returns (ending run_until_complete) once _stop_event is set and
        # the async-with blocks above have unwound, so by the time the thread has
        # joined the loop is idle and safe to close from this (the calling) thread.
        self._loop.close()
