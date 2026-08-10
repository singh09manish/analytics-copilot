from copilot.agent import prompts


def test_prompt_version_bumped():
    assert prompts.PROMPT_VERSION == "v3"


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
