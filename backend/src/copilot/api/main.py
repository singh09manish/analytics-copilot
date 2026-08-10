import functools
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot import auth
from copilot.agent.pipeline import ChatResponse, answer_question
from copilot.agent.prompts import PROMPT_VERSION
from copilot.request_log import log_request

logger = logging.getLogger(__name__)

# Must match Settings.jwt_secret's default in copilot/config.py (and .env.example)
# exactly: startup refuses to run with this literal in force. It's published in
# the repo, so anyone with the repo can forge a {"role": "admin"} token and get
# the unmasked Snowflake session -- this JWT is the whole authorization boundary.
_DEFAULT_JWT_SECRET = "dev-secret-change-me"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from copilot.config import get_settings

    if get_settings().jwt_secret == _DEFAULT_JWT_SECRET:
        raise RuntimeError(
            "Refusing to start: JWT_SECRET is still the published default "
            f"({_DEFAULT_JWT_SECRET!r}). This token is the authorization boundary "
            "for role-scoped Snowflake access. Generate real demo credentials with "
            "`cd backend && uv run python ../scripts/gen_demo_users.py` and set "
            "JWT_SECRET plus DEMO_ANALYST_PASSWORD_HASH/DEMO_ADMIN_PASSWORD_HASH "
            "in .env before starting this app.")
    yield
    executor = getattr(app.state, "executor", None)
    if executor is not None:
        executor.close()


app = FastAPI(title="Analytics Copilot", lifespan=lifespan)
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


_deps_lock = threading.Lock()


def _deps():
    """Lazily build request-scoped singletons on app.state, on first use.

    Sync endpoints run in FastAPI's threadpool, so concurrent first requests could
    previously observe app.state half-built (provider set, sf_admin/executor not
    yet) and crash with AttributeError. `_deps_lock` serializes construction (one
    Snowflake/MCP build, not N racing ones), and every dependency is built into a
    local first and published to app.state only as the LAST step, so no thread can
    ever read a partial state.

    If MCP construction fails, we still publish a complete state with
    executor=None (a deliberate, logged fallback to direct Snowflake execution)
    instead of leaving app.state half-set: the old code let that exception escape
    _deps() entirely, 500-ing the first request while still having already set
    provider/sf_ro/sf_admin/sf_writer/executor=None beforehand -- so the
    hasattr(state, "provider") short-circuit above made every later request
    silently skip MCP forever with no log line explaining why.
    """
    if hasattr(app.state, "provider"):
        return app.state
    with _deps_lock:
        if hasattr(app.state, "provider"):  # lost the race while waiting for the lock
            return app.state
        from copilot.config import get_settings
        from copilot.llm.provider import AnthropicProvider
        from copilot.snowflake_client import SnowflakeClient

        provider = AnthropicProvider()
        sf_ro = SnowflakeClient(role="COPILOT_APP_RO")
        sf_admin = SnowflakeClient(role="COPILOT_ADMIN")
        sf_writer = SnowflakeClient(role="COPILOT_APP_WRITER")
        executor = None
        if get_settings().use_mcp:
            from copilot.mcp_client import McpExecutor

            try:
                executor = McpExecutor()
            except Exception:  # degrade to direct execution, never 500 the request
                logger.warning(
                    "MCP executor failed to start; falling back to direct "
                    "Snowflake execution for this process.", exc_info=True)
                executor = None
        app.state.provider = provider
        app.state.sf_ro = sf_ro
        app.state.sf_admin = sf_admin
        app.state.sf_writer = sf_writer
        app.state.executor = executor
    return app.state


def _require_role(request: Request) -> str:
    return auth.require_role(request)


def _require_identity(request: Request) -> tuple[str, str]:
    """Bearer -> (role, email), decoding the token exactly once.

    Any failure -- missing header, bad signature, expired token, or a
    validly-signed token missing an expected claim -- maps to 401, never an
    uncaught exception. (A previous version decoded twice: once via
    auth.require_role, again to recover the email, which meant a token expiring
    between the two decodes raised AuthError uncaught, and an unguarded
    payload["sub"] raised KeyError uncaught -- both surfaced as 500s.)
    """
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        payload = auth.decode_token(header.removeprefix("Bearer "))
        return payload["role"], payload["sub"]
    except (auth.AuthError, KeyError) as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/auth/login")
def login(req: LoginRequest) -> dict:
    role = auth.authenticate(req.email, req.password)
    if role is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    # Normalize once and use the SAME normalized form everywhere (token subject
    # AND response body). Previously the token carried the raw login string while
    # the response echoed strip().lower() -- so "analyst@demo", "ANALYST@demo",
    # and "  analyst@demo  " each minted a distinct checkpointer identity (see
    # scoped_conversation_id in chat()) for what should be one account.
    email = req.email.strip().lower()
    return {"token": auth.create_token(role, email), "role": role, "email": email}


@app.post("/chat")
def chat(req: ChatRequest, identity: tuple[str, str] = Depends(_require_identity)) -> ChatResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")
    role, email = identity
    # ':' is the checkpointer thread-key separator below; rejecting it from client
    # input keeps that key unambiguous without relying on "email never contains a
    # colon" as an assumption that could break with a future non-demo identity
    # source.
    if req.conversation_id is not None and ":" in req.conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id must not contain ':'")
    state = _deps()
    sf = state.sf_admin if role == "admin" else state.sf_ro
    # Roles map exactly -- analyst -> COPILOT_APP_RO, admin -> COPILOT_ADMIN -- for
    # query EXECUTION too, not just retrieval. Snowflake's masking policies CASE on
    # CURRENT_ROLE(), so routing an admin's query execution through the RO role
    # would come back masked. Derived from the verified JWT only (fail-closed to
    # the masked role for anything else), never from the request body: the MCP
    # server is a local subprocess owned by this API, so this value is
    # API-supplied, not user-supplied (the server keeps its own allowlist anyway,
    # as defense in depth).
    sf_role = "COPILOT_ADMIN" if role == "admin" else "COPILOT_APP_RO"
    # functools.partial keeps the executor's (sql) -> (columns, rows) calling shape
    # that the graph depends on; the role is bound here rather than threaded
    # through the graph/executor protocol.
    executor = (functools.partial(state.executor.run_query, role=sf_role)
                if state.executor else None)
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
