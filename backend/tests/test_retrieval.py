from copilot.retrieval import RetrievedContext, _keyword_fallback

CARDS = [
    ("GOLD.FACT_MACHINE_UTILIZATION", "downtime hours uptime fractions machine day"),
    ("GOLD.DIM_TREATMENT_CENTER", "center region country city beds email"),
    ("GOLD.FACT_SERVICE_TICKET", "ticket severity category resolution repair"),
]
TERMS = [
    ("downtime percent", "downtime_hours over total hours"),
    ("MTTR", "mean time to repair average resolution_hours"),
]


def test_keyword_fallback_ranks_by_overlap():
    ctx = _keyword_fallback("Which centers had the most downtime?", CARDS, TERMS, 2, 1)
    assert isinstance(ctx, RetrievedContext)
    assert ctx.mode == "keyword"
    assert "FACT_MACHINE_UTILIZATION" in ctx.schema_cards[0]
    assert "downtime percent" in ctx.glossary[0]
