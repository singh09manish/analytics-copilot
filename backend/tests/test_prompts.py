import re

import yaml

from copilot.agent import prompts
from copilot.config import REPO_ROOT

SCHEMA_CARDS_PATH = REPO_ROOT / "data" / "ai_library" / "schema_cards.yaml"


def test_prompt_version_bumped():
    assert prompts.PROMPT_VERSION == "v4"


def test_plan_system_mentions_intents():
    s = prompts.plan_system()
    for intent in ("data_query", "glossary_lookup", "smalltalk", "unsupported"):
        assert intent in s


def test_plan_system_carries_table_inventory():
    """The planner classifies with no retrieval step, so the tables and their
    columns have to be in its own prompt. Without them it judged requests for
    specific columns -- 'treatment centers with their contact emails', the RBAC
    demo question -- as unsupported, 5 times out of 5."""
    s = prompts.plan_system()
    for table in ("DIM_TREATMENT_CENTER", "DIM_MACHINE", "FACT_MACHINE_UTILIZATION",
                  "FACT_SERVICE_TICKET"):
        assert table in s
    assert "contact_email" in s


def test_plan_system_defers_sensitivity_to_access_control():
    """A field looking private is not the planner's call to make -- masking and
    role grants decide that downstream. Left to itself the planner refused
    contact-email questions outright, which silently broke the masking demo."""
    s = prompts.plan_system().lower()
    assert "sensitive" in s or "private" in s
    assert "downstream" in s


def test_user_with_history_includes_last_turns():
    hist = [("q1", "a1"), ("q2", "a2"), ("q3", "a3"), ("q4", "a4")]
    out = prompts.user_with_history("current?", hist)
    assert "current?" in out and "q4" in out and "q2" in out
    assert "q1" not in out  # only last 3 turns


def _schema_card_tokens() -> dict[str, set[str]]:
    """table_name -> every identifier-shaped token anywhere in that table's card
    text (lower-cased). A broad membership set, not a strict "Columns:" parse --
    good enough to prove a claimed name is real, and immune to schema_cards.yaml's
    prose (Sample questions, etc.) wording changing around it."""
    cards = yaml.safe_load(SCHEMA_CARDS_PATH.read_text())
    return {
        entry["table_name"]: set(re.findall(r"[a-z][a-z0-9_]*", entry["card"].lower()))
        for entry in cards
    }


def _inventory_bullets(text: str) -> list[tuple[str, str]]:
    """(table, text-after-'--') for each '- GOLD.<TABLE>: ... -- ...' bullet in
    plan_system()'s table inventory. A bullet with no '--' (no literal column
    list, just a description) yields an empty second element."""
    start = text.index("fair game to ask about:") + len("fair game to ask about:")
    end = text.index("Classify intent as exactly one of:")
    section = text[start:end].strip()
    bullets = re.split(r"\n(?=- GOLD\.)", section)
    out = []
    for bullet in bullets:
        m = re.match(r"- GOLD\.(\w+):(.*)", bullet, re.DOTALL)
        assert m, f"could not parse a table bullet out of plan_system(): {bullet!r}"
        table, rest = m.group(1), m.group(2)
        _, _, col_text = rest.partition("--")
        out.append((table, col_text.replace("\n", " ")))
    return out


def _claimed_columns(col_text: str) -> list[str]:
    """Single-word, comma-separated tokens only -- a prose fragment like
    "log date" or "planned/delivered fractions" (used deliberately for the two
    tables whose real column list doesn't fit a bare enumeration) contains a
    space or slash and never matches, so it is correctly skipped rather than
    mis-flagged as a bogus column."""
    return [tok for raw in col_text.split(",")
            if re.fullmatch(r"[a-z][a-z0-9_]*", (tok := raw.strip()))]


def test_plan_system_table_inventory_columns_exist_in_schema_cards():
    """Final review, Important finding I10: six wrong column names -- opened_date
    and software_version (real: go_live_date, sw_version), a fabricated DIM_DATE
    `week`, a fabricated FACT_SERVICE_TICKET `status` (real: category), and
    uptime/downtime described in minutes when the columns are `*_hours` -- shipped
    live in this exact prompt (commit c27eb47) and were caught only by a human
    reading docs against the warehouse; nothing failed hermetically. This walks
    every table bullet in plan_system()'s inventory and asserts each literal
    column name it claims actually appears on that table's card in
    data/ai_library/schema_cards.yaml, so a future typo here fails a fast, offline
    test instead of waiting for a live run or a manual review."""
    tokens_by_table = _schema_card_tokens()
    checked = 0
    for table, col_text in _inventory_bullets(prompts.plan_system()):
        card_table = f"GOLD.{table}"
        assert card_table in tokens_by_table, (
            f"plan_system() names {card_table}, which does not exist in "
            "data/ai_library/schema_cards.yaml")
        for column in _claimed_columns(col_text):
            checked += 1
            assert column in tokens_by_table[card_table], (
                f"plan_system() claims column {column!r} on {card_table}, but "
                f"{column!r} does not appear anywhere on that table's card in "
                "data/ai_library/schema_cards.yaml -- verify the real column "
                "name against the warehouse and fix the prompt")
    # A sanity floor on the parser itself, not just the columns it found: today's
    # inventory has 49 literal column names across DIM_TREATMENT_CENTER (8),
    # DIM_MACHINE (7), DIM_DATE (7), FACT_MACHINE_UTILIZATION (8),
    # FACT_SERVICE_TICKET (10), and V_CENTER_MONTHLY_KPIS (9) -- every GOLD
    # table now names its columns explicitly instead of hiding them behind
    # prose ("planned/delivered fractions", "pre-aggregated monthly KPIs"),
    # which is what let `parts_cost` go missing and fail the
    # parts-cost-by-model golden case as `unsupported`. A count far below 49
    # means the bullet/column parser broke, not that the prompt got shorter --
    # and a silently-empty parser would make every assertion above vacuously
    # true.
    assert checked >= 40, (
        f"only parsed {checked} column names out of plan_system()'s table "
        "inventory; expected at least 40 -- the bullet parser in this test may "
        "be broken rather than the prompt actually having fewer columns")
