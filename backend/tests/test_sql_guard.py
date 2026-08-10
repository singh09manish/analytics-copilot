import pytest

from copilot.sql_guard import SqlGuardError, validate


def test_valid_select_gets_limit_injected():
    out = validate("SELECT model, COUNT(*) FROM GOLD.DIM_MACHINE GROUP BY model")
    assert "LIMIT 1000" in out


def test_existing_limit_preserved():
    out = validate("SELECT * FROM GOLD.DIM_MACHINE LIMIT 5")
    assert "LIMIT 5" in out and "1000" not in out


def test_cte_and_union_allowed():
    sql = """WITH x AS (SELECT center_id FROM GOLD.DIM_TREATMENT_CENTER)
             SELECT * FROM x UNION ALL SELECT center_id FROM GOLD.DIM_TREATMENT_CENTER"""
    assert "LIMIT" in validate(sql)


@pytest.mark.parametrize("bad", [
    "DROP TABLE GOLD.DIM_MACHINE",
    "INSERT INTO GOLD.DIM_MACHINE VALUES (1)",
    "UPDATE GOLD.DIM_MACHINE SET model='x'",
    "DELETE FROM GOLD.DIM_MACHINE",
    "CREATE TABLE GOLD.T (a INT)",
    "SELECT 1; SELECT 2",
])
def test_ddl_dml_and_multistatement_rejected(bad):
    with pytest.raises(SqlGuardError):
        validate(bad)


def test_bronze_schema_rejected():
    with pytest.raises(SqlGuardError, match="schema"):
        validate("SELECT * FROM BRONZE.RAW_CENTERS")


def test_unqualified_table_rejected():
    with pytest.raises(SqlGuardError, match="qualify"):
        validate("SELECT * FROM DIM_MACHINE")


def test_fully_qualified_ok():
    out = validate("SELECT * FROM MEDTECH_ANALYTICS.GOLD.DIM_MACHINE")
    assert "LIMIT 1000" in out


def test_wrong_database_rejected():
    with pytest.raises(SqlGuardError, match="database"):
        validate("SELECT * FROM OTHERDB.GOLD.DIM_MACHINE")


@pytest.mark.parametrize("bad", [
    "SELECT * INTO COPILOT.EXFIL FROM GOLD.DIM_MACHINE",
    "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
    "SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())",
    "SELECT * FROM GOLD.DIM_MACHINE, LATERAL SILVER.MY_UDTF(1)",
    "SELECT SILVER.SECRET_FN(machine_id) FROM GOLD.DIM_MACHINE",
    'SELECT * FROM "gold"."dim_machine"',
])
def test_bypass_vectors_rejected(bad):
    with pytest.raises(SqlGuardError):
        validate(bad)


def test_limit_null_replaced_with_cap():
    assert "LIMIT 1000" in validate("SELECT * FROM GOLD.DIM_MACHINE LIMIT NULL")


def test_huge_limit_clamped():
    out = validate("SELECT * FROM GOLD.DIM_MACHINE LIMIT 99999")
    assert "LIMIT 1000" in out and "99999" not in out


def test_quoted_uppercase_schema_allowed():
    assert "LIMIT 1000" in validate('SELECT * FROM "GOLD"."DIM_MACHINE"')


def test_except_and_intersect_allowed():
    out = validate(
        "SELECT center_id FROM GOLD.DIM_TREATMENT_CENTER "
        "EXCEPT SELECT center_id FROM GOLD.DIM_MACHINE")
    assert "LIMIT 1000" in out


def test_nested_cte_bare_reference_rejected():
    sql = ("SELECT (SELECT COUNT(*) FROM (WITH SECRETS AS (SELECT 1 AS a) "
           "SELECT a FROM SECRETS)) AS n, s.* FROM SECRETS s")
    with pytest.raises(SqlGuardError):
        validate(sql)


@pytest.mark.parametrize("bad", [
    "SELECT * FROM GOLD.MY_UDTF(1)",
    "SELECT * FROM COPILOT.SOME_PROC(1)",
    "SELECT * FROM GOLD.DIM_MACHINE d, GOLD.SECRET_LEAK_FN(d.machine_id) f",
])
def test_bare_udtf_as_table_rejected(bad):
    with pytest.raises(SqlGuardError):
        validate(bad)


# --- Final review, Critical finding C1: the COPILOT schema holds REQUEST_LOG (every
# user's raw question, intent, generated SQL and role) and FEEDBACK (free-text
# comments), and COPILOT_APP_RO could read them. Layer 1 must not let LLM-generated
# SQL name that schema at all -- nothing in the LLM path needs it (retrieval and the
# MCP metadata tools use app-authored SQL that never reaches validate()).


@pytest.mark.parametrize("bad", [
    "SELECT question, user_role, conversation_id FROM COPILOT.REQUEST_LOG",
    "SELECT * FROM MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG",
    "SELECT comment, rating FROM MEDTECH_ANALYTICS.COPILOT.FEEDBACK",
    "SELECT * FROM COPILOT.EVAL_RESULTS",
    "SELECT * FROM COPILOT.SCHEMA_CARDS",
    'SELECT * FROM "COPILOT"."REQUEST_LOG"',
    ("SELECT m.model FROM GOLD.DIM_MACHINE m "
     "JOIN COPILOT.REQUEST_LOG r ON r.request_id = m.machine_id"),
    ("WITH leaked AS (SELECT question FROM MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG) "
     "SELECT * FROM leaked"),
])
def test_copilot_schema_rejected(bad):
    with pytest.raises(SqlGuardError, match="schema"):
        validate(bad)


def test_gold_is_the_only_allowed_schema():
    from copilot.sql_guard import ALLOWED_SCHEMAS

    assert ALLOWED_SCHEMAS == {"GOLD"}


# --- Final review, Important finding I2: the side-effect denylist lived only in the
# MCP server, so it vanished whenever the executor was unavailable or USE_MCP=false.
# It now runs in layer 1 too, on the parsed tree, so obfuscated spellings that defeat
# a raw-text regex are still caught.


@pytest.mark.parametrize("bad", [
    "SELECT GET_DDL('view', 'GOLD.DIM_TREATMENT_CENTER') AS d",
    "SELECT GET_DDL/*x*/('TABLE','GOLD.DIM_MACHINE')",
    "SELECT GET_DDL -- x\n('TABLE','GOLD.DIM_MACHINE')",
    'SELECT "GET_DDL"(\'TABLE\',\'GOLD.DIM_MACHINE\')',
    "SELECT SYSTEM$CANCEL_ALL_QUERIES() AS x",
    "SELECT SYSTEM$WAIT(600, 'SECONDS') AS x",
    "SELECT model FROM GOLD.DIM_MACHINE WHERE SYSTEM$WAIT(600, 'SECONDS') IS NOT NULL",
])
def test_side_effecting_functions_rejected_at_layer1(bad):
    with pytest.raises(SqlGuardError, match="side-effect"):
        validate(bad)


def test_string_literal_naming_a_denied_function_is_not_a_call():
    """A literal that merely contains the text must not trip the denylist -- the
    check walks the parsed tree, so this is exp.Literal, not a function call."""
    assert "LIMIT 1000" in validate(
        "SELECT model FROM GOLD.DIM_MACHINE WHERE model = 'GET_DDL(x)'")


def test_deny_side_effects_shares_layer1_implementation():
    """Layer 2 (mcp_server) calls this helper rather than owning a second copy of
    the rule, so the two layers cannot drift apart."""
    from copilot.sql_guard import deny_side_effects

    deny_side_effects("SELECT model FROM GOLD.DIM_MACHINE LIMIT 1000")  # must not raise
    with pytest.raises(SqlGuardError, match="side-effect"):
        deny_side_effects("SELECT SYSTEM$CANCEL_ALL_QUERIES()")
