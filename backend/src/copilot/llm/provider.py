from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from copilot.config import get_settings


class LLMResult(BaseModel):
    value: Any
    tokens_in: int = 0
    tokens_out: int = 0


class LLMProvider(Protocol):
    def structured(self, system: str, user: str, schema: type[BaseModel],
                   max_tokens: int = 1500) -> LLMResult: ...

    def text(self, system: str, user: str, max_tokens: int = 1000) -> LLMResult: ...


class AnthropicProvider:
    """Claude via the Anthropic API. Forced tool-use for schema-checked structured output."""

    def __init__(self, client: anthropic.Anthropic | None = None):
        settings = get_settings()
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key, max_retries=2
        )
        self._model = settings.app_model

    def _call_structured(self, system: str, user: str, schema: type[BaseModel],
                         max_tokens: int) -> tuple[BaseModel | None, Any, str | None]:
        resp = self._client.messages.create(
            model=self._model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": "emit", "description": f"Emit a {schema.__name__}",
                    "input_schema": schema.model_json_schema()}],
            tool_choice={"type": "tool", "name": "emit"},
        )
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            error_text = f"no tool_use block in response (stop_reason={getattr(resp, 'stop_reason', None)})"
            return None, resp, error_text
        try:
            return schema.model_validate(block.input), resp, None
        except ValidationError as e:
            return None, resp, str(e)

    def structured(self, system: str, user: str, schema: type[BaseModel],
                   max_tokens: int = 1500) -> LLMResult:
        value, resp, error_text = self._call_structured(system, user, schema, max_tokens)
        if value is not None:
            return LLMResult(value=value, tokens_in=resp.usage.input_tokens,
                             tokens_out=resp.usage.output_tokens)
        retry_user = (f"{user}\n\nYour previous output failed schema validation:\n{error_text}\n"
                      f"Emit a corrected {schema.__name__}.")
        value, resp, error_text = self._call_structured(system, retry_user, schema, max_tokens)
        if value is None:
            raise ValueError(f"LLM output schema-invalid twice for {schema.__name__}")
        return LLMResult(value=value, tokens_in=resp.usage.input_tokens,
                         tokens_out=resp.usage.output_tokens)

    def text(self, system: str, user: str, max_tokens: int = 1000) -> LLMResult:
        resp = self._client.messages.create(
            model=self._model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResult(value=out, tokens_in=resp.usage.input_tokens,
                         tokens_out=resp.usage.output_tokens)
