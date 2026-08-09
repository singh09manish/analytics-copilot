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
