import json

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
