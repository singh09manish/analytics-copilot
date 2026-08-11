"""recall@k is the metric that matters here: of the tables this question genuinely
needs, how many did retrieval put in front of the model? A missing join table is
the classic silent RAG failure -- the SQL comes back plausible and wrong."""
from copilot.eval.retrieval_eval import RetrievalCase, load_retrieval_cases, score_retrieval
from copilot.retrieval import RetrievedContext


def _ctx(*tables):
    return RetrievedContext(schema_cards=[f"{t}: some card text" for t in tables],
                            glossary=[])


def test_full_recall():
    c = RetrievalCase(id="x", question="q",
                      must_retrieve=["GOLD.DIM_MACHINE", "GOLD.FACT_MACHINE_UTILIZATION"])
    recall, missing = score_retrieval(c, _ctx("GOLD.DIM_MACHINE",
                                              "GOLD.FACT_MACHINE_UTILIZATION"))
    assert recall == 1.0 and missing == []


def test_partial_recall_names_what_was_missed():
    c = RetrievalCase(id="x", question="q",
                      must_retrieve=["GOLD.DIM_MACHINE", "GOLD.FACT_MACHINE_UTILIZATION"])
    recall, missing = score_retrieval(c, _ctx("GOLD.DIM_MACHINE"))
    assert recall == 0.5 and missing == ["GOLD.FACT_MACHINE_UTILIZATION"]


def test_matching_is_case_insensitive():
    c = RetrievalCase(id="x", question="q", must_retrieve=["gold.dim_machine"])
    assert score_retrieval(c, _ctx("GOLD.DIM_MACHINE"))[0] == 1.0


def test_retrieval_cases_load_and_reference_real_tables():
    cases = load_retrieval_cases()
    assert len(cases) >= 8
    known = {"GOLD.DIM_MACHINE", "GOLD.DIM_TREATMENT_CENTER", "GOLD.DIM_DATE",
             "GOLD.FACT_MACHINE_UTILIZATION", "GOLD.FACT_SERVICE_TICKET",
             "GOLD.V_CENTER_MONTHLY_KPIS"}
    for c in cases:
        for t in c.must_retrieve:
            assert t.upper() in known, f"{c.id} references unknown table {t}"


# --- extra coverage: zero found, no false 1.0 on an unmatched miss, and the exact
# known-failure case (a cross-table question that missed DIM_MACHINE until a join
# hint was added to the utilization card) is present in the real data file.


def test_zero_recall_when_nothing_matches():
    c = RetrievalCase(id="x", question="q", must_retrieve=["GOLD.DIM_MACHINE"])
    recall, missing = score_retrieval(c, _ctx("GOLD.FACT_SERVICE_TICKET"))
    assert recall == 0.0 and missing == ["GOLD.DIM_MACHINE"]


def test_missing_is_sorted():
    c = RetrievalCase(id="x", question="q",
                      must_retrieve=["GOLD.DIM_TREATMENT_CENTER", "GOLD.DIM_MACHINE"])
    _recall, missing = score_retrieval(c, _ctx())
    assert missing == ["GOLD.DIM_MACHINE", "GOLD.DIM_TREATMENT_CENTER"]


def test_retrieval_set_has_at_least_two_cross_table_cases():
    cases = load_retrieval_cases()
    cross_table = [c for c in cases if len(c.must_retrieve) >= 2]
    assert len(cross_table) >= 2


def test_retrieval_set_includes_the_known_utilization_by_model_failure():
    """A Phase 1 question missed DIM_MACHINE until a join hint was added to the
    utilization card -- this must stay a permanent regression check."""
    cases = load_retrieval_cases()
    matches = [c for c in cases if "GOLD.DIM_MACHINE" in c.must_retrieve
              and "GOLD.FACT_MACHINE_UTILIZATION" in c.must_retrieve]
    assert matches, "no retrieval case covers the DIM_MACHINE join-hint regression"
