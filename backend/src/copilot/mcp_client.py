"""Sync wrapper over the async MCP stdio client, for use inside the agent graph."""
import asyncio
import concurrent.futures
import json
import logging
import sys
import threading
import time

from copilot.config import REPO_ROOT

logger = logging.getLogger(__name__)

CALL_TIMEOUT = 60
# How often a blocked caller re-checks whether the background loop has died. The
# subprocess can die *after* a call is submitted, in which case the coroutine is
# abandoned on a stopped loop and its future never resolves -- polling is what turns
# that into a fast McpError instead of a full CALL_TIMEOUT stall.
_DEATH_POLL_INTERVAL = 0.05


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
        # Set when a tool call fails at the transport level while the background loop
        # is still parked in `_main`. Killing the server subprocess does not always
        # unwind stdio_client's task group -- the reader task can end cleanly, leaving
        # `_main` waiting on `_stop_event` forever while every call_tool fails with
        # "Connection closed". Without this flag the executor looks alive and the API
        # would keep it (and keep failing every request) for the process lifetime.
        self._broken = False
        # Set the instant the background loop stops for any reason -- clean close, a
        # cancelled startup, or the server subprocess dying under us. Once set, no
        # coroutine can ever run on `_loop` again, so callers must fail fast rather
        # than wait out their timeout on a future nobody will ever resolve.
        self._dead = threading.Event()
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
        except Exception:  # the thread is dying either way; record why
            logger.warning("MCP background loop exited with an error.", exc_info=True)
        finally:
            # Publish "this executor is dead" BEFORE the thread exits. Previously the
            # thread just ended: the loop was left stopped-but-not-closed, `_closed`
            # stayed False, and every later run_query burned its full 60s timeout on a
            # coroutine scheduled onto a loop that would never run it again -- twice
            # per request thanks to the graph's repair edge.
            self._session = None
            self._dead.set()
            self._ready.set()  # unblock a __init__ still waiting on a session

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
        except Exception as exc:  # startup failures surface to __init__ via _start_error
            if self._ready.is_set():
                # Post-startup death (the server subprocess exited under us). __init__
                # is long gone, so nobody will read _start_error -- log it, or this
                # failure is completely invisible while every request degrades.
                logger.warning(
                    "MCP session ended unexpectedly; this executor is now dead and "
                    "will report failures immediately: %s", exc, exc_info=True)
            else:
                self._start_error = McpError(f"failed to start MCP session: {exc}")
                self._ready.set()
        finally:
            self._session = None

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
        try:
            if self._main_task is not None:
                self._loop.call_soon_threadsafe(self._main_task.cancel)
            else:
                self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass  # the loop already stopped for good; nothing left to cancel
        self._thread.join(timeout=10)
        if not self._loop.is_closed():
            self._loop.close()
        self._closed = True
        self._dead.set()

    def is_broken(self) -> bool:
        """True once this executor can never serve another query.

        Covers all three ways it can stop working: the background loop exited
        (`_dead`), a caller closed it (`_closed`), or the stdio transport under a
        still-running loop is gone (`_broken`, set by run_query). Callers use it to
        swap in a fresh executor instead of holding a corpse for the process lifetime.
        """
        return self._dead.is_set() or self._closed or self._broken or self._session is None

    def _mark_broken(self, reason: str) -> None:
        if not self._broken:
            self._broken = True
            logger.warning(
                "MCP transport is unusable; this executor is marked broken so it can "
                "be replaced: %s", reason)

    def _run(self, coro, timeout: float = CALL_TIMEOUT):
        if self.is_broken():
            coro.close()  # never awaited; closing it avoids a "never awaited" warning
            raise McpError(
                "MCP session is not available (the server subprocess has exited)")
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as e:  # loop closed between the check and the submit
            coro.close()
            raise McpError(f"MCP session is not available: {e}") from e
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # An abandoned call_tool would otherwise stay outstanding and the
                # Snowflake query behind it would keep running (and keep burning
                # credits) long after the user was told the request failed.
                future.cancel()
                raise McpError(f"MCP tool call timed out after {timeout}s")
            try:
                return future.result(timeout=min(_DEATH_POLL_INTERVAL, remaining))
            except concurrent.futures.TimeoutError:
                if self._dead.is_set() and not future.done():
                    # The loop stopped while this call was in flight: the coroutine
                    # will never be resumed, so waiting out `timeout` is pointless.
                    future.cancel()
                    raise McpError(
                        "MCP session died while the query was in flight") from None

    def run_query(self, sql: str, role: str = "COPILOT_APP_RO") -> tuple[list, list]:
        """`role` is forwarded as a tool argument so the server executes under the
        caller's actual Snowflake role (masking policies CASE on CURRENT_ROLE()) --
        see mcp_server/server.py's run_query/_sf() for the server-side allowlist."""

        async def call():
            return await self._session.call_tool("run_query", {"sql": sql, "role": role})

        try:
            result = self._run(call())
        except McpError:
            raise
        except Exception as exc:  # transport-level failure, not a tool-level one
            # Anything escaping call_tool itself is a transport/protocol failure (the
            # SDK raises "Connection closed" once the subprocess is gone). Tool-level
            # failures never reach here: they come back as a CallToolResult with
            # isError set and are turned into McpError by _parse_tool_result below.
            self._mark_broken(str(exc))
            raise McpError(f"MCP transport failed: {exc}") from exc
        return _parse_tool_result(result)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass  # already dead: the loop stopped on its own, nothing to signal
        self._thread.join(timeout=10)
        # `_main` returns (ending run_until_complete) once _stop_event is set and
        # the async-with blocks above have unwound, so by the time the thread has
        # joined the loop is idle and safe to close from this (the calling) thread.
        if not self._loop.is_closed():
            self._loop.close()
        self._dead.set()
