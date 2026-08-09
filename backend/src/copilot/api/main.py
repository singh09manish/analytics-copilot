import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot import auth
from copilot.agent.pipeline import ChatResponse, answer_question
from copilot.agent.prompts import PROMPT_VERSION
from copilot.request_log import log_request

app = FastAPI(title="Analytics Copilot")
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


class LoginRequest(BaseModel):
    email: str
    password: str


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class FeedbackRequest(BaseModel):
    request_id: str
    conversation_id: str | None = None
    rating: str  # "up" | "down"
    comment: str | None = None


def _deps():
    if not hasattr(app.state, "provider"):
        from copilot.config import get_settings
        from copilot.llm.provider import AnthropicProvider
        from copilot.snowflake_client import SnowflakeClient

        app.state.provider = AnthropicProvider()
        app.state.sf_ro = SnowflakeClient(role="COPILOT_APP_RO")
        app.state.sf_admin = SnowflakeClient(role="COPILOT_ADMIN")
        app.state.sf_writer = SnowflakeClient(role="COPILOT_APP_WRITER")
        app.state.executor = None
        if get_settings().use_mcp:
            from copilot.mcp_client import McpExecutor

            app.state.executor = McpExecutor()
    return app.state


def _require_role(request: Request) -> str:
    return auth.require_role(request)


def _require_identity(request: Request) -> tuple[str, str]:
    """Like auth.require_role, but also returns the authenticated email so callers
    can namespace anything keyed by client-supplied identifiers (see chat())."""
    role = auth.require_role(request)
    header = request.headers.get("authorization", "")
    payload = auth.decode_token(header.removeprefix("Bearer "))
    return role, payload["sub"]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/auth/login")
def login(req: LoginRequest) -> dict:
    role = auth.authenticate(req.email, req.password)
    if role is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": auth.create_token(role, req.email), "role": role,
            "email": req.email.strip().lower()}


@app.post("/chat")
def chat(req: ChatRequest, identity: tuple[str, str] = Depends(_require_identity)) -> ChatResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")
    role, email = identity
    state = _deps()
    sf = state.sf_admin if role == "admin" else state.sf_ro
    executor = state.executor.run_query if state.executor else None
    # The LangGraph checkpointer is a process-global InMemorySaver keyed by thread_id.
    # A client-supplied conversation_id must never be used as that key directly, or
    # one user could supply another user's conversation_id and read their history.
    # Namespacing by the authenticated email keeps each user's memory isolated even
    # when two clients reuse the same conversation_id.
    scoped_conversation_id = f"{email}:{req.conversation_id}" if req.conversation_id is not None else None
    start = time.monotonic()
    resp = answer_question(req.question, state.provider, sf,
                           conversation_id=scoped_conversation_id, executor=executor)
    log_request(state.sf_writer, request_id=resp.request_id or "",
                conversation_id=req.conversation_id, user_role=role,
                question=req.question, response=resp,
                e2e_ms=int((time.monotonic() - start) * 1000))
    return resp


@app.post("/feedback")
def feedback(req: FeedbackRequest, role: str = Depends(_require_role)) -> dict:
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be up|down")
    state = _deps()
    try:
        state.sf_writer.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.FEEDBACK "
            "(conversation_id, request_id, rating, comment, prompt_version) "
            "VALUES (%s, %s, %s, %s, %s)",
            (req.conversation_id, req.request_id, req.rating,
             (req.comment or "")[:2000], PROMPT_VERSION))
    except Exception as e:
        raise HTTPException(status_code=503, detail="feedback store unavailable") from e
    return {"status": "recorded"}
