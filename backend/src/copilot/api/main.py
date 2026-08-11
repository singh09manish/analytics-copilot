import functools
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot import auth, metrics
from copilot.agent.pipeline import ChatResponse, answer_question
from copilot.agent.prompts import PROMPT_VERSION
from copilot.request_log import log_request

logger = logging.getLogger(__name__)

# Must match Settings.jwt_secret's default in copilot/config.py (and .env.example)
# exactly: startup refuses to run with this literal in force. It's published in
# the repo, so anyone with the repo can forge a {"role": "admin"} token and get
# the unmasked Snowflake session -- this JWT is the whole authorization boundary.
_DEFAULT_JWT_SECRET = "dev-secret-change-me"
# HS256 keys shorter than the hash output are weak (RFC 7518 3.2, and PyJWT warns
# about it). Rejecting only the published literal let JWT_SECRET=x through, which is
# no harder to guess than the default it was meant to replace. gen_demo_users.py
# emits a 58-char hex secret, so this never fires on a correctly-provisioned .env.
_MIN_JWT_SECRET_BYTES = 32


@asynccontextmanager
async def lifespan(app: FastAPI):
    from copilot.config import get_settings

    secret = get_settings().jwt_secret
    if secret == _DEFAULT_JWT_SECRET:
        raise RuntimeError(
            "Refusing to start: JWT_SECRET is still the published default "
            f"({_DEFAULT_JWT_SECRET!r}). This token is the authorization boundary "
            "for role-scoped Snowflake access. Generate real demo credentials with "
            "`cd backend && uv run python ../scripts/gen_demo_users.py` and set "
            "JWT_SECRET plus DEMO_ANALYST_PASSWORD_HASH/DEMO_ADMIN_PASSWORD_HASH "
            "in .env before starting this app.")
    if len(secret.encode()) < _MIN_JWT_SECRET_BYTES:
        raise RuntimeError(
            f"Refusing to start: JWT_SECRET is only {len(secret.encode())} bytes; "
            f"HS256 needs at least {_MIN_JWT_SECRET_BYTES}. This token is the "
            "authorization boundary for role-scoped Snowflake access -- generate one "
            "with `cd backend && uv run python ../scripts/gen_demo_users.py`.")
    yield
    executor = getattr(app.state, "executor", None)
    if executor is not None:
        executor.close()


app = FastAPI(title="Analytics Copilot", lifespan=lifespan)

# CloudFront routes /api/* to the ALB with the path unchanged, so every route is
# mounted under /api. /healthz stays at the root for the ALB's own health check,
# which talks to the task directly and never goes through CloudFront.


# CORS origins are resolved once at import time (not request time) because
# get_settings() is @lru_cache'd. This is correct for containerized deployment,
# where environment variables are set before the process starts. For local testing
# that needs to vary CORS_ALLOW_ORIGINS between tests, set the env var before
# importing this module (e.g., in pytest fixtures that use monkeypatch), not after.
def _cors_origins() -> list[str]:
    from copilot.config import get_settings

    return get_settings().cors_origin_list()


app.add_middleware(
    CORSMiddleware, allow_origins=_cors_origins(),
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
        executor = _build_executor() if get_settings().use_mcp else None
        app.state.provider = provider
        app.state.sf_ro = sf_ro
        app.state.sf_admin = sf_admin
        app.state.sf_writer = sf_writer
        app.state.executor = executor
    return app.state


# A rebuild happens inside a live request, so it gets a much shorter readiness
# budget than the cold-start build: better to degrade to direct execution for this
# request than to hold a user's /chat open for a full minute.
_MCP_REBUILD_READY_TIMEOUT = 15


def _build_executor(ready_timeout: float | None = None):
    """Construct an McpExecutor, or return None after logging why it failed."""
    from copilot.mcp_client import McpExecutor

    try:
        return McpExecutor() if ready_timeout is None else McpExecutor(ready_timeout=ready_timeout)
    except Exception:  # degrade to direct execution, never 500 the request
        logger.warning(
            "MCP executor failed to start; falling back to direct Snowflake "
            "execution. Layer 2 re-validation is out of the path until it recovers.",
            exc_info=True)
        return None


def _live_executor(state):
    """Return a usable executor, replacing one whose subprocess has died.

    Without this, a dead McpExecutor stayed on app.state for the process lifetime:
    every /chat then blocked on it (twice, thanks to the graph's repair edge) and
    the API never recovered. The rebuild is attempted once per detected death; if it
    also fails we fall back to direct execution, which is only acceptable because the
    side-effect denylist now lives in sql_guard (layer 1) rather than only in the MCP
    server -- see the C1/I2 fix.
    """
    executor = getattr(state, "executor", None)
    if executor is None:
        return None
    is_broken = getattr(executor, "is_broken", None)
    if not callable(is_broken) or not is_broken():
        return executor
    with _deps_lock:
        if state.executor is not executor:  # another thread already replaced it
            return state.executor
        logger.warning("MCP executor is dead; rebuilding it for subsequent requests.")
        try:
            executor.close()
        except Exception:  # best-effort teardown of a corpse
            logger.warning("Closing the dead MCP executor failed.", exc_info=True)
        state.executor = _build_executor(ready_timeout=_MCP_REBUILD_READY_TIMEOUT)
    return state.executor


# Ops-table column lists are FIXED by warehouse/bootstrap.sql. Kept next to the
# INSERT (as in request_log.py) so a hermetic test can assert
# len(COLUMNS) == sql.count("%s") == len(params).
FEEDBACK_COLUMNS = ("conversation_id", "request_id", "rating", "comment", "prompt_version")
FEEDBACK_INSERT_SQL = (
    "INSERT INTO MEDTECH_ANALYTICS.COPILOT.FEEDBACK "
    f"({', '.join(FEEDBACK_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(FEEDBACK_COLUMNS))})")

# Admin Console reads. All three go through state.sf_admin (COPILOT_ADMIN) --
# COPILOT_APP_RO cannot read these tables at the Snowflake grant level (see the
# comment above the COPILOT_ADMIN grant in warehouse/bootstrap.sql), so running
# them on the wrong session would pass hermetically and fail live.
#
# ADMIN_OVERVIEW_SQL uses GROUPING SETS to get the grand total (intent IS NULL)
# and the per-intent breakdown out of REQUEST_LOG in one query, as the brief
# asks for, rather than one query per number.
ADMIN_OVERVIEW_SQL = (
    "SELECT intent, COUNT(*) AS total, "
    "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
    "MEDIAN(e2e_ms) AS p50_latency_ms "
    "FROM MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG "
    "GROUP BY GROUPING SETS ((), (intent))")
ADMIN_FEEDBACK_COUNTS_SQL = (
    "SELECT rating, COUNT(*) AS cnt "
    "FROM MEDTECH_ANALYTICS.COPILOT.FEEDBACK GROUP BY rating")

# Console-facing column subsets (request_id, created_at, ... below) are our own
# JSON contract, not a mirror of the full table -- but every name in them must
# still be a real column from warehouse/bootstrap.sql's REQUEST_LOG/FEEDBACK DDL.
ADMIN_REQUESTS_COLUMNS = ("request_id", "created_at", "user_role", "intent", "status",
                          "error_type", "e2e_ms", "sql_text", "question")
ADMIN_REQUESTS_SQL = (
    f"SELECT {', '.join(ADMIN_REQUESTS_COLUMNS)} "
    "FROM MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG ORDER BY created_at DESC LIMIT %s")
ADMIN_FEEDBACK_COLUMNS = ("feedback_id", "created_at", "conversation_id", "request_id",
                         "rating", "comment")
ADMIN_FEEDBACK_SQL = (
    f"SELECT {', '.join(ADMIN_FEEDBACK_COLUMNS)} "
    "FROM MEDTECH_ANALYTICS.COPILOT.FEEDBACK ORDER BY created_at DESC LIMIT %s")

_ADMIN_LIMIT_MIN = 1
_ADMIN_LIMIT_MAX = 500


def _clamp_admin_limit(limit: int) -> int:
    return max(_ADMIN_LIMIT_MIN, min(_ADMIN_LIMIT_MAX, limit))


def _rows_to_dicts(cols: list[str], rows: list[tuple], wanted: tuple[str, ...]) -> list[dict]:
    """Map a Snowflake result to our JSON contract by column name, not position.

    Snowflake returns unquoted identifiers upper-cased, so this looks columns up
    case-insensitively; a name missing from the result (e.g. a fake test double
    that returns unrelated canned columns) maps to None rather than raising.
    """
    idx = {c.upper(): i for i, c in enumerate(cols)}
    return [{name: (row[idx[name.upper()]] if name.upper() in idx else None) for name in wanted}
            for row in rows]


def _scoped_conversation_id(email: str, conversation_id: str | None) -> str | None:
    """Checkpointer thread key: the client's conversation_id namespaced by identity.

    The LangGraph checkpointer is process-global and keyed by thread_id, so a
    client-supplied conversation_id must never be the key directly -- one user could
    supply another's and read their history. None stays None: an anonymous turn gets
    no memory at all rather than a shared one.
    """
    return f"{email}:{conversation_id}" if conversation_id is not None else None


def _logged_conversation_id(email: str, conversation_id: str | None) -> str:
    """Audit key for REQUEST_LOG/FEEDBACK: always identity-scoped, never None.

    The ops tables have no user column (their DDL is fixed) and REQUEST_LOG records
    only user_role, so logging the raw client value made rows unattributable: two
    users who both send conversation_id "1" got correctly isolated agent memory and
    conflated audit rows. Scoping the logged value the same way the thread key is
    scoped names the actor, and keeps the two views of a conversation consistent.
    """
    return f"{email}:{conversation_id if conversation_id is not None else ''}"


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


def _require_admin(identity: tuple[str, str] = Depends(_require_identity)) -> tuple[str, str]:
    """Gate for the admin-only ops endpoints, built on top of `_require_identity`.

    An anonymous caller never reaches the role check: `_require_identity` already
    raises 401 first. An authenticated non-admin gets 403, not 404 -- the resource
    exists and the caller is authenticated, so pretending otherwise is security
    theatre that costs debuggability.
    """
    role, _email = identity
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return identity


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/auth/login")
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


@app.post("/api/chat")
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
    # through the graph/executor protocol. _live_executor replaces one whose MCP
    # subprocess has died instead of handing the graph a corpse to block on.
    mcp_executor = _live_executor(state)
    executor = (functools.partial(mcp_executor.run_query, role=sf_role)
                if mcp_executor is not None else None)
    # The LangGraph checkpointer is a process-global InMemorySaver keyed by thread_id.
    # A client-supplied conversation_id must never be used as that key directly, or
    # one user could supply another user's conversation_id and read their history.
    # Namespacing by the authenticated email keeps each user's memory isolated even
    # when two clients reuse the same conversation_id.
    scoped_conversation_id = _scoped_conversation_id(email, req.conversation_id)
    start = time.monotonic()
    resp = answer_question(req.question, state.provider, sf,
                           conversation_id=scoped_conversation_id, executor=executor)
    e2e_ms = int((time.monotonic() - start) * 1000)
    log_request(state.sf_writer, request_id=resp.request_id or "",
                conversation_id=_logged_conversation_id(email, req.conversation_id),
                user_role=role, question=req.question, response=resp,
                e2e_ms=e2e_ms)
    outcome = resp.error_type or "ok"
    metrics.emit("Answered", 1, "Count", role=role, outcome=outcome, intent=resp.intent)
    metrics.emit("LatencyMs", e2e_ms, "Milliseconds", role=role, outcome=outcome)
    metrics.emit("RetrievalMs", resp.retrieval_ms or 0, "Milliseconds",
                 role=role, mode=resp.retrieval_mode)
    metrics.emit("TokensTotal", (resp.tokens_in or 0) + (resp.tokens_out or 0),
                 "Count", role=role)
    return resp


@app.post("/api/feedback")
def feedback(req: FeedbackRequest,
             identity: tuple[str, str] = Depends(_require_identity)) -> dict:
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be up|down")
    _role, email = identity
    state = _deps()
    try:
        state.sf_writer.run_query(
            FEEDBACK_INSERT_SQL,
            (_logged_conversation_id(email, req.conversation_id), req.request_id,
             req.rating, (req.comment or "")[:2000], PROMPT_VERSION))
    except Exception as e:
        raise HTTPException(status_code=503, detail="feedback store unavailable") from e
    return {"status": "recorded"}


@app.get("/api/admin/overview")
def admin_overview(_identity: tuple[str, str] = Depends(_require_admin)) -> dict:
    state = _deps()
    try:
        cols, rows = state.sf_admin.run_query(ADMIN_OVERVIEW_SQL)
        fcols, frows = state.sf_admin.run_query(ADMIN_FEEDBACK_COUNTS_SQL)
    except Exception as e:
        raise HTTPException(status_code=503, detail="admin store unavailable") from e

    idx = {c.upper(): i for i, c in enumerate(cols)}
    total_requests = 0
    errors = 0
    p50_latency_ms = 0
    by_intent: dict[str, int] = {}
    for row in rows:
        intent = row[idx["INTENT"]] if "INTENT" in idx else None
        total = int(row[idx["TOTAL"]] or 0) if "TOTAL" in idx and row[idx["TOTAL"]] is not None else 0
        if intent is None:
            total_requests = total
            errors = int(row[idx["ERRORS"]] or 0) if "ERRORS" in idx else 0
            p50_latency_ms = row[idx["P50_LATENCY_MS"]] if "P50_LATENCY_MS" in idx else 0
        else:
            by_intent[str(intent)] = total

    fidx = {c.upper(): i for i, c in enumerate(fcols)}
    feedback_up = feedback_down = 0
    for row in frows:
        rating = row[fidx["RATING"]] if "RATING" in fidx else None
        cnt = int(row[fidx["CNT"]] or 0) if "CNT" in fidx else 0
        if rating == "up":
            feedback_up = cnt
        elif rating == "down":
            feedback_down = cnt

    error_rate = (errors / total_requests) if total_requests else 0.0
    return {
        "total_requests": total_requests,
        "error_rate": error_rate,
        "p50_latency_ms": p50_latency_ms or 0,
        "feedback_up": feedback_up,
        "feedback_down": feedback_down,
        "by_intent": by_intent,
    }


@app.get("/api/admin/requests")
def admin_requests(limit: int = 50,
                   _identity: tuple[str, str] = Depends(_require_admin)) -> list[dict]:
    state = _deps()
    n = _clamp_admin_limit(limit)
    try:
        cols, rows = state.sf_admin.run_query(ADMIN_REQUESTS_SQL, (n,))
    except Exception as e:
        raise HTTPException(status_code=503, detail="admin store unavailable") from e
    return _rows_to_dicts(cols, rows, ADMIN_REQUESTS_COLUMNS)


@app.get("/api/admin/feedback")
def admin_feedback(limit: int = 50,
                   _identity: tuple[str, str] = Depends(_require_admin)) -> list[dict]:
    state = _deps()
    n = _clamp_admin_limit(limit)
    try:
        cols, rows = state.sf_admin.run_query(ADMIN_FEEDBACK_SQL, (n,))
    except Exception as e:
        raise HTTPException(status_code=503, detail="admin store unavailable") from e
    return _rows_to_dicts(cols, rows, ADMIN_FEEDBACK_COLUMNS)
