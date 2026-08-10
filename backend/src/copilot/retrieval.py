import logging
import time

from pydantic import BaseModel

from copilot.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

EMBED = "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', %s)"


class RetrievedContext(BaseModel):
    schema_cards: list[str]
    glossary: list[str]
    retrieval_ms: int = 0
    # "vector" (Cortex) or "keyword" (fallback). Carried through the graph into
    # ChatResponse.retrieval_mode so a silent degradation to keyword ranking is
    # visible, rather than the app quietly contradicting the README's "Cortex vector
    # retrieval" claim with nothing in any log or response to show for it.
    mode: str = "vector"


def _keyword_fallback(question: str, cards: list[tuple], terms: list[tuple],
                      k_cards: int, k_terms: int) -> RetrievedContext:
    words = {w.strip("?,.").lower() for w in question.split() if len(w) > 3}

    def score(text: str) -> int:
        return sum(1 for w in words if w in text.lower())

    ranked_cards = sorted(cards, key=lambda c: score(c[0] + " " + c[1]), reverse=True)
    ranked_terms = sorted(terms, key=lambda t: score(t[0] + " " + t[1]), reverse=True)
    return RetrievedContext(
        schema_cards=[f"{c[0]}: {c[1]}" for c in ranked_cards[:k_cards]],
        glossary=[f"{t[0]}: {t[1]}" for t in ranked_terms[:k_terms]],
        mode="keyword",
    )


def retrieve(question: str, sf: SnowflakeClient, k_cards: int = 3,
             k_terms: int = 5) -> RetrievedContext:
    start = time.monotonic()
    try:
        _, card_rows = sf.run_query(
            "SELECT table_name, card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS "
            f"ORDER BY VECTOR_COSINE_SIMILARITY(embedding, {EMBED}) DESC LIMIT {k_cards}",
            (question,),
        )
        _, term_rows = sf.run_query(
            "SELECT term || ': ' || definition FROM MEDTECH_ANALYTICS.COPILOT.GLOSSARY "
            f"ORDER BY VECTOR_COSINE_SIMILARITY(embedding, {EMBED}) DESC LIMIT {k_terms}",
            (question,),
        )
        ctx = RetrievedContext(
            schema_cards=[f"{r[0]}: {r[1]}" for r in card_rows],
            glossary=[r[0] for r in term_rows],
        )
    except Exception:
        logger.warning(
            "Cortex vector retrieval failed; falling back to keyword ranking for "
            "this request. Results will be less relevant until Cortex is reachable.",
            exc_info=True)
        _, cards = sf.run_query(
            "SELECT table_name, card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS")
        _, terms = sf.run_query(
            "SELECT term, definition FROM MEDTECH_ANALYTICS.COPILOT.GLOSSARY")
        ctx = _keyword_fallback(question, cards, terms, k_cards, k_terms)
    ctx.retrieval_ms = int((time.monotonic() - start) * 1000)
    return ctx
