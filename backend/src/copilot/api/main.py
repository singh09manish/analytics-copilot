from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot.agent.pipeline import ChatResponse, answer_question

app = FastAPI(title="Analytics Copilot")
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


def _deps():
    if not hasattr(app.state, "provider"):
        from copilot.llm.provider import AnthropicProvider
        from copilot.snowflake_client import SnowflakeClient

        app.state.provider = AnthropicProvider()
        app.state.sf_ro = SnowflakeClient(role="COPILOT_APP_RO")
    return app.state.provider, app.state.sf_ro


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest) -> ChatResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")
    provider, sf = _deps()
    return answer_question(req.question, provider, sf)
