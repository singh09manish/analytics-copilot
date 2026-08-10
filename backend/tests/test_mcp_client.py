import asyncio
import json
import threading
import time

from copilot.mcp_client import _parse_tool_result


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class ToolResult:
    def __init__(self, payload, is_error=False):
        self.content = [Block(json.dumps(payload) if isinstance(payload, dict) else payload)]
        self.isError = is_error
        self.structuredContent = payload if isinstance(payload, dict) else None


def test_parse_structured_content():
    cols, rows = _parse_tool_result(ToolResult({"columns": ["A"], "rows": [[1]]}))
    assert cols == ["A"] and rows == [[1]]


def test_parse_error_raises_with_message():
    import pytest

    from copilot.mcp_client import McpError
    with pytest.raises(McpError, match="invalid identifier"):
        _parse_tool_result(ToolResult("SQL compilation error: invalid identifier", True))


# --- Finding 4: the success/parsing path must never leak a raw parser
# exception (IndexError/JSONDecodeError/AttributeError) -- everything short of
# the isError branch should surface as McpError so the graph's repair loop
# sees a tool-shaped failure, not a stack trace from this module.


class NoStructuredContentResult:
    """A result with no structuredContent/structured_content, forcing the
    JSON-text fallback path -- same as what the real MCP SDK returns today
    (see task-6-report.md's captured raw CallToolResult)."""

    def __init__(self, content, is_error=False):
        self.content = content
        self.isError = is_error
        self.structuredContent = None


def test_parse_empty_content_raises_mcp_error():
    import pytest

    from copilot.mcp_client import McpError
    with pytest.raises(McpError, match="empty result"):
        _parse_tool_result(NoStructuredContentResult(content=[]))


def test_parse_malformed_json_raises_mcp_error():
    import pytest

    from copilot.mcp_client import McpError
    with pytest.raises(McpError, match="could not parse"):
        _parse_tool_result(NoStructuredContentResult(content=[Block("not valid json{")]))


def test_parse_non_text_content_block_raises_mcp_error():
    import pytest

    from copilot.mcp_client import McpError

    class ImageBlock:
        def __init__(self):
            self.type = "image"
            # deliberately no .text attribute

    with pytest.raises(McpError, match="non-text content block"):
        _parse_tool_result(NoStructuredContentResult(content=[ImageBlock()]))


def test_parse_missing_columns_key_raises_mcp_error():
    import pytest

    from copilot.mcp_client import McpError
    with pytest.raises(McpError, match="could not parse"):
        _parse_tool_result(ToolResult({"rows": [[1]]}))


# --- Task 7 follow-up review, Finding 1: McpExecutor.run_query must carry the
# Snowflake role through to the tool call as an explicit argument (never hardcode
# a single role), so masking policies that CASE on CURRENT_ROLE() apply correctly.
# These exercise just the argument-shape contract against a stub session -- the
# real stdio transport is covered by test_mcp_stdio_roundtrip.py.


def _run_against_stub_session(stub_session, _on_executor=None, **run_query_kwargs):
    """Drive McpExecutor.run_query against a stub session without spawning the
    real stdio subprocess: McpExecutor normally builds `_loop`/`_session` via a
    background-thread handshake in __init__, but run_query only needs a running
    event loop (for run_coroutine_threadsafe) and a `_session.call_tool`.

    `_on_executor` hands the constructed executor back to the caller so tests can
    inspect its liveness state even when run_query raises.
    """
    from copilot.mcp_client import McpExecutor

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ex = McpExecutor.__new__(McpExecutor)
    ex._loop = loop
    ex._session = stub_session
    # Liveness bookkeeping run_query consults before submitting to the loop (see
    # McpExecutor.is_broken); __init__ normally sets these.
    ex._dead = threading.Event()
    ex._closed = False
    ex._broken = False
    if _on_executor is not None:
        _on_executor(ex)
    try:
        return ex.run_query("SELECT 1", **run_query_kwargs)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


class _StubSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return ToolResult({"columns": ["A"], "rows": [[1]]})


def test_run_query_sends_role_as_explicit_tool_argument():
    stub = _StubSession()
    cols, rows = _run_against_stub_session(stub, role="COPILOT_ADMIN")
    assert stub.calls == [("run_query", {"sql": "SELECT 1", "role": "COPILOT_ADMIN"})]
    assert cols == ["A"] and rows == [[1]]


def test_run_query_defaults_to_copilot_app_ro_role():
    stub = _StubSession()
    _run_against_stub_session(stub)
    assert stub.calls == [("run_query", {"sql": "SELECT 1", "role": "COPILOT_APP_RO"})]


# --- Final review, Critical finding C2: a transport-level failure (the server
# subprocess died) must mark the executor broken so the API can replace it, while a
# tool-level failure (a guard rejection, say) must NOT -- the session is fine, the
# SQL wasn't.


class _TransportDeadSession:
    """call_tool itself raises, the way the MCP SDK reports a closed connection."""

    async def call_tool(self, name, args):
        raise RuntimeError("Connection closed")


def test_transport_failure_marks_executor_broken():
    import pytest

    from copilot.mcp_client import McpError

    holder = {}

    def capture(ex):
        holder["ex"] = ex

    with pytest.raises(McpError, match="transport failed"):
        _run_against_stub_session(_TransportDeadSession(), _on_executor=capture)
    assert holder["ex"].is_broken()


class _ToolErrorSession:
    async def call_tool(self, name, args):
        return ToolResult("rejected by SQL guard: schema COPILOT is not allowed", True)


def test_tool_level_error_does_not_mark_executor_broken():
    import pytest

    from copilot.mcp_client import McpError

    holder = {}

    with pytest.raises(McpError, match="rejected by SQL guard"):
        _run_against_stub_session(_ToolErrorSession(),
                                  _on_executor=lambda ex: holder.__setitem__("ex", ex))
    assert not holder["ex"].is_broken()


def test_run_query_on_a_dead_executor_raises_immediately():
    """No event loop at all: is_broken() short-circuits before anything is submitted,
    so the caller gets McpError now instead of burning the full call timeout."""
    import pytest

    from copilot.mcp_client import McpError, McpExecutor

    ex = McpExecutor.__new__(McpExecutor)
    ex._loop = None
    ex._session = None
    ex._dead = threading.Event()
    ex._dead.set()
    ex._closed = False
    ex._broken = False
    start = time.monotonic()
    with pytest.raises(McpError, match="not available"):
        ex.run_query("SELECT 1")
    assert time.monotonic() - start < 1
