"""Deterministic SQL guard: layer 1 of 3 (app validator, MCP server, Snowflake role)."""
import sqlglot
from sqlglot import expressions as exp

# GOLD only. COPILOT is deliberately NOT reachable from LLM-generated SQL: since
# Phase 2 it holds REQUEST_LOG (every user's raw question, intent, generated SQL and
# role), FEEDBACK (free-text comments) and EVAL_RESULTS, and COPILOT_APP_RO can read
# that schema. Allowing it here would have let any analyst ask "show me everything in
# MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG" and read every other user's questions --
# through all three defense layers. Nothing in the LLM path needs COPILOT: retrieval
# (retrieval.py) and the MCP metadata tools (mcp_server/server.py) reach SCHEMA_CARDS
# and GLOSSARY with app-authored SQL that never passes through validate().
ALLOWED_SCHEMAS = {"GOLD"}
ALLOWED_DBS = {"", "MEDTECH_ANALYTICS"}
_BANNED_NAMES = ("Insert", "Update", "Delete", "Drop", "Create", "Alter",
                 "Merge", "TruncateTable", "Command", "Grant", "Into")
BANNED = tuple(getattr(exp, n) for n in _BANNED_NAMES if hasattr(exp, n))
DEFAULT_LIMIT = 1000

# Scalar functions that read or mutate warehouse state rather than the GOLD data the
# copilot is scoped to. GET_DDL leaks object definitions the role's SELECT grants
# don't cover; SYSTEM$... spans cancellation (SYSTEM$CANCEL_ALL_QUERIES) and warehouse
# pinning (SYSTEM$WAIT(600,'SECONDS')). These live in layer 1 rather than only in the
# MCP server so they hold when USE_MCP=false or the MCP executor is unavailable.
DENIED_FUNCTIONS = frozenset({"GET_DDL"})
DENIED_FUNCTION_PREFIXES = ("SYSTEM$",)
SIDE_EFFECT_REASON = "side-effecting or metadata functions are not allowed"


class SqlGuardError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _ident_value(ident: exp.Identifier | None) -> str:
    """Quoted identifiers keep exact case (Snowflake semantics); unquoted fold to upper."""
    if ident is None:
        return ""
    return ident.name if ident.quoted else ident.name.upper()


def _is_denied_function(node: exp.Expression) -> bool:
    """True for a parsed call to a denied function, in any of its written forms.

    Matching on the PARSED node rather than raw text is what makes this
    unbypassable: sqlglot relocates comments when it re-serializes (so
    `GET_DDL/*x*/(...)` and `GET_DDL -- x\\n(...)` defeat a raw-text regex) and it
    resolves a double-quoted identifier like `"GET_DDL"(...)` to the same builtin
    Snowflake would call. All three parse to one exp.Anonymous/exp.Func node named
    GET_DDL. Conversely a string literal that merely contains the text "GET_DDL("
    parses to exp.Literal and is correctly ignored.
    """
    if not isinstance(node, (exp.Anonymous, exp.Func)):
        return False
    name = (node.name or "").upper()
    return name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES)


def deny_side_effects(sql: str) -> None:
    """Re-run only the side-effect check over already-validated SQL (defense layer 2).

    `validate()` performs this same check inline, so this exists for the MCP tool
    server, which re-validates whatever SQL it is handed by any stdio client --
    including clients that never went through layer 1. Shares `_is_denied_function`
    with `validate()` so the two layers can never drift apart.
    """
    try:
        tree = sqlglot.parse_one(sql, read="snowflake")
    except sqlglot.errors.ParseError as e:
        raise SqlGuardError(f"SQL failed to parse: {e}") from e
    for node in tree.walk():
        if _is_denied_function(node):
            raise SqlGuardError(SIDE_EFFECT_REASON)


def validate(sql: str) -> str:
    try:
        statements = sqlglot.parse(sql, read="snowflake")
    except sqlglot.errors.ParseError as e:
        raise SqlGuardError(f"SQL failed to parse: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlGuardError("exactly one SELECT statement is required")
    stmt = statements[0]
    set_op = getattr(exp, "SetOperation", exp.Union)
    if not isinstance(stmt, (exp.Select, set_op)):
        raise SqlGuardError("only SELECT statements are allowed")
    if stmt.args.get("into"):
        raise SqlGuardError("SELECT INTO is not allowed")
    for node in stmt.walk():
        if isinstance(node, BANNED):
            raise SqlGuardError(f"{node.key.upper()} is not allowed")
        if _is_denied_function(node):
            raise SqlGuardError(SIDE_EFFECT_REASON)
        if hasattr(exp, "TableFromRows") and isinstance(node, exp.TableFromRows):
            raise SqlGuardError("table functions are not allowed")
        if isinstance(node, exp.Lateral):
            raise SqlGuardError("LATERAL is not allowed")
        if isinstance(node, exp.Dot) and any(
            isinstance(v, (exp.Anonymous, exp.Func)) for v in node.args.values()
        ):
            raise SqlGuardError("qualified function calls are not allowed")
    with_node = stmt.args.get("with") or stmt.args.get("with_")
    cte_names = {c.alias_or_name.upper() for c in (with_node.expressions if with_node else [])}
    for t in stmt.find_all(exp.Table):
        if isinstance(t.this, (exp.Anonymous, exp.Func)):
            raise SqlGuardError("function calls as tables are not allowed")
        schema = _ident_value(t.args.get("db"))
        db = _ident_value(t.args.get("catalog"))
        if not schema:
            if t.name.upper() in cte_names:
                continue
            raise SqlGuardError(f"qualify table {t.name} as SCHEMA.TABLE")
        if db not in ALLOWED_DBS:
            raise SqlGuardError(f"database {db} is not allowed")
        if schema not in ALLOWED_SCHEMAS:
            raise SqlGuardError(f"schema {schema} is not allowed (use GOLD)")
    limit_node = stmt.args.get("limit")
    keep = False
    if limit_node is not None:
        lit = limit_node.expression
        if isinstance(lit, exp.Literal) and lit.is_int and 1 <= int(lit.this) <= DEFAULT_LIMIT:
            keep = True
    if not keep:
        stmt.set("limit", None)
        stmt = stmt.limit(DEFAULT_LIMIT)
    return stmt.sql(dialect="snowflake")
