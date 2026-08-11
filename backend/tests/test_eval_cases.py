import pytest

from copilot.eval.cases import EvalCase, load_cases


def test_golden_set_loads_and_is_substantial():
    cases = load_cases()
    assert len(cases) >= 30
    assert all(isinstance(c, EvalCase) for c in cases)


def test_ids_are_unique():
    ids = [c.id for c in load_cases()]
    assert len(ids) == len(set(ids))


def test_every_intent_is_covered():
    """expect_refused cases (the safety- cases) deliberately carry no `intent` --
    which layer refuses them is not pinned -- so they are excluded here rather
    than requiring the sentinel None to join the set of real intents."""
    intents = {c.intent for c in load_cases() if not c.expect_refused}
    assert intents == {"data_query", "glossary_lookup", "smalltalk", "unsupported"}


def test_duplicate_ids_are_rejected(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "cases:\n"
        "  - {id: a, question: q1, intent: smalltalk}\n"
        "  - {id: a, question: q2, intent: smalltalk}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(p)


def test_safety_cases_expect_refusal():
    """The safety- cases are the point of the harness -- if one ever starts
    returning data, a defense layer regressed. They assert the outcome
    (expect_refused), not which layer produces it -- see cases.py and the
    top-of-file comment in golden.yaml for why pinning the layer (intent=
    data_query, expect_error_type=validation) was the wrong design: the first
    live run proved the planner legitimately refuses these one layer earlier
    than the guard, and a case that cannot fail because it never reaches the
    guard is not testing the guard."""
    safety = [c for c in load_cases() if c.id.startswith("safety-")]
    assert len(safety) >= 5
    assert all(c.expect_refused for c in safety)
    assert all(c.expect_error_type is None for c in safety)


def test_golden_cases_only_reference_real_gold_tables():
    """Ground every expectation in the real warehouse -- catches a typo'd or
    invented table name before it ever reaches a live run."""
    known = {"GOLD.DIM_TREATMENT_CENTER", "GOLD.DIM_MACHINE", "GOLD.DIM_DATE",
             "GOLD.FACT_MACHINE_UTILIZATION", "GOLD.FACT_SERVICE_TICKET",
             "GOLD.V_CENTER_MONTHLY_KPIS"}
    for c in load_cases():
        for expectation in c.expect_sql_contains:
            if expectation.upper().startswith("GOLD."):
                assert expectation.upper() in known, (
                    f"{c.id} references unknown table {expectation}")
