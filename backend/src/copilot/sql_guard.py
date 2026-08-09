"""Deterministic SQL guard: layer 1 of 3 (app validator, MCP server, Snowflake role)."""
import sqlglot
from sqlglot import expressions as exp

ALLOWED_SCHEMAS = {"GOLD", "COPILOT"}
ALLOWED_DBS = {"", "MEDTECH_ANALYTICS"}
_BANNED_NAMES = ("Insert", "Update", "Delete", "Drop", "Create", "Alter",
                 "Merge", "TruncateTable", "Command", "Grant", "Into")
BANNED = tuple(getattr(exp, n) for n in _BANNED_NAMES if hasattr(exp, n))
DEFAULT_LIMIT = 1000


class SqlGuardError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _ident_value(ident: exp.Identifier | None) -> str:
    """Quoted identifiers keep exact case (Snowflake semantics); unquoted fold to upper."""
    if ident is None:
        return ""
    return ident.name if ident.quoted else ident.name.upper()


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
