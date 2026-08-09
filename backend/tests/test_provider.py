from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from copilot.llm.provider import AnthropicProvider
from copilot.llm.schemas import SqlDraft


def _resp(tool_input):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input=tool_input)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def test_structured_parses_valid_output():
    client = MagicMock()
    client.messages.create.return_value = _resp(
        {"sql": "SELECT 1", "tables_used": ["GOLD.DIM_MACHINE"], "assumptions": []}
    )
    p = AnthropicProvider(client=client)
    out = p.structured(system="s", user="u", schema=SqlDraft)
    assert out.value.sql == "SELECT 1"
    assert out.tokens_in == 100
    tool = client.messages.create.call_args.kwargs["tools"][0]
    assert tool["input_schema"] == SqlDraft.model_json_schema()


def test_structured_retries_once_on_validation_error():
    client = MagicMock()
    client.messages.create.side_effect = [
        _resp({"wrong_field": True}),
        _resp({"sql": "SELECT 2", "tables_used": [], "assumptions": ["retried"]}),
    ]
    p = AnthropicProvider(client=client)
    out = p.structured(system="s", user="u", schema=SqlDraft)
    assert out.value.sql == "SELECT 2"
    assert client.messages.create.call_count == 2
    retry_user = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "validation" in retry_user.lower()


def test_structured_raises_after_second_failure():
    client = MagicMock()
    client.messages.create.side_effect = [_resp({"bad": 1}), _resp({"bad": 2})]
    p = AnthropicProvider(client=client)
    with pytest.raises(ValueError, match="schema-invalid"):
        p.structured(system="s", user="u", schema=SqlDraft)


def test_structured_handles_missing_tool_use_block():
    client = MagicMock()
    client.messages.create.side_effect = [
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I refuse")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        ),
        _resp({"sql": "SELECT 3", "tables_used": [], "assumptions": []}),
    ]
    p = AnthropicProvider(client=client)
    out = p.structured(system="s", user="u", schema=SqlDraft)
    assert out.value.sql == "SELECT 3"
    assert client.messages.create.call_count == 2


def test_structured_raises_when_both_missing_tool_use():
    client = MagicMock()
    client.messages.create.side_effect = [
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I refuse")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        ),
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Still refuse")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        ),
    ]
    p = AnthropicProvider(client=client)
    with pytest.raises(ValueError, match="schema-invalid"):
        p.structured(system="s", user="u", schema=SqlDraft)


def test_text_joins_text_blocks():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Hello "),
            SimpleNamespace(type="text", text="world"),
        ],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )
    p = AnthropicProvider(client=client)
    out = p.text(system="s", user="u")
    assert out.value == "Hello world"
