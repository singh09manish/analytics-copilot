"""Sync wrapper over the async MCP stdio client, for use inside the agent graph."""
import asyncio
import json
import sys
import threading
import time

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
        text = "MCP tool error"
        if result.content:
            block_text = getattr(result.content[0], "text", None)
            if block_text is not None:
                text = block_text
        raise McpError(text)
    try:
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = getattr(result, "structured_content", None)
        if payload is None:
            if not result.content:
                raise McpError("MCP tool returned an empty result with no content")
            block = result.content[0]
            text = getattr(block, "text", None)
            if text is None:
                kind = getattr(block, "type", type(block).__name__)
                raise McpError(f"MCP tool returned a non-text content block ({kind})")
            payload = json.loads(text)
        if "result" in payload and "columns" not in payload:  # FastMCP wraps plain returns
            payload = payload["result"]
        return list(payload["columns"]), [list(r) for r in payload["rows"]]
    except McpError:
        raise
    except (IndexError, KeyError, TypeError, ValueError, AttributeError) as exc:
        # Covers json.JSONDecodeError (a ValueError subclass) for malformed tool
        # output, and shape mismatches (missing "columns"/"rows", non-dict payload,
        # non-iterable rows) -- surface these as McpError so the graph's repair
        # loop sees a tool-shaped failure instead of a raw parser traceback.
        raise McpError(f"could not parse MCP tool result: {exc}") from exc


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

    def __init__(self, ready_timeout: float = 60, *, server_args: list[str] | None = None):
        # `server_args` overrides the spawned command's argv (default: the real
        # mcp_server/server.py); it exists so tests can point the transport at a
        # deliberately-hanging script to exercise the startup-timeout teardown
        # path without waiting on (or risking) the real server/Snowflake.
        self._server_args = server_args
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._session = None
        self._stop_event = None
        self._start_error = None
        self._main_task = None
        self._closed = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=ready_timeout):
            self._abort_startup()
            raise McpError(f"timed out after {ready_timeout}s starting MCP server subprocess")
        if self._start_error is not None:
            self._abort_startup()
            raise self._start_error

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._main_task = self._loop.create_task(self._main())
        try:
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass  # expected when _abort_startup()/close() cancels a hung startup

    async def _main(self):
        import os

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stop_event = asyncio.Event()
        args = self._server_args
        if args is None:
            args = [str(REPO_ROOT / "mcp_server" / "server.py")]
        params = StdioServerParameters(
            command=sys.executable,
            args=args,
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

    def _abort_startup(self):
        """Best-effort teardown for a construction that never became ready.

        Cancelling `_main_task` (rather than just stopping the loop) is what
        actually unwinds the `async with stdio_client(...)`/`ClientSession(...)`
        blocks and kills the spawned subprocess -- merely stopping the loop can
        leave `_main` (and its subprocess) suspended mid-flight, orphaning it.
        """
        if self._loop.is_closed():
            return
        # `_main_task` is set as the first statement the background thread runs;
        # give it a brief, bounded grace period in case __init__'s ready_timeout
        # is so short the thread hasn't been scheduled yet.
        deadline = time.monotonic() + 2
        while self._main_task is None and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._main_task is not None:
            self._loop.call_soon_threadsafe(self._main_task.cancel)
        else:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        if not self._loop.is_closed():
            self._loop.close()
        self._closed = True

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

    def run_query(self, sql: str) -> tuple[list, list]:
        async def call():
            return await self._session.call_tool("run_query", {"sql": sql})

        return _parse_tool_result(self._run(call()))

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=10)
        # `_main` returns (ending run_until_complete) once _stop_event is set and
        # the async-with blocks above have unwound, so by the time the thread has
        # joined the loop is idle and safe to close from this (the calling) thread.
        if not self._loop.is_closed():
            self._loop.close()
