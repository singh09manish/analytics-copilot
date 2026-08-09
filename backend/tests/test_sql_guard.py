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
