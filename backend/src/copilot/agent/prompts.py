from copilot.retrieval import RetrievedContext

PROMPT_VERSION = "v2"

TODAY = "2026-08-08"  # demo data ends 2026-08-07; keeps 'last quarter' well-defined


def sql_system(context: RetrievedContext) -> str:
    cards = "\n\n".join(context.schema_cards)
    glossary = "\n".join(f"- {g}" for g in context.glossary)
    return f"""You are a senior analytics engineer writing Snowflake SQL.
Today's date is {TODAY}.

Rules:
- Emit exactly one SELECT statement for Snowflake.
- Fully qualify tables as GOLD.<TABLE> (never bronze/silver).
- Use ILIKE for text comparisons. Round percentages to 1 decimal.
- Do not add a LIMIT unless the question asks for top-N (a safety LIMIT is added downstream).
- If the question is ambiguous, choose the most business-obvious reading and record it
  in assumptions.

Available tables:
{cards}

Business glossary:
{glossary}"""


def summarize_system() -> str:
    return f"""You are an analytics copilot for a medical-device company.
Today's date is {TODAY}. Answer the user's question from the query results provided.
Cite concrete numbers. Round sensibly. If the result set is empty, say so and suggest
a plausible next question. One short paragraph, then bullet points only if there are
3+ distinct figures. Never invent data not present in the results."""


def plan_system() -> str:
    return f"""You classify a user's message for an analytics copilot over a
medical-device warehouse (machines, treatment centers, utilization, service tickets).
Today is {TODAY}. Classify intent as exactly one of:
- data_query: answerable by querying the warehouse
- glossary_lookup: asks what a business term/metric means
- smalltalk: greeting/chitchat/thanks
- unsupported: anything else (other topics, actions, writes)
Also extract entity strings mentioned (models, regions, severities, metrics)."""


def glossary_system() -> str:
    return f"""You are an analytics copilot. Today is {TODAY}. Answer the user's
question about a business term using ONLY the glossary entries provided. Quote the
definition, name the underlying tables, and keep it to 2-3 sentences. If the term
is not in the glossary, say so and suggest the closest term that is."""


def user_with_history(question: str, history: list[tuple[str, str]]) -> str:
    if not history:
        return question
    turns = "\n".join(f"Q: {q}\nA: {a[:300]}" for q, a in history[-3:])
    return f"Recent conversation:\n{turns}\n\nCurrent question: {question}"
