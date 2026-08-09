from copilot.agent import prompts


def test_prompt_version_bumped():
    assert prompts.PROMPT_VERSION == "v2"


def test_plan_system_mentions_intents():
    s = prompts.plan_system()
    for intent in ("data_query", "glossary_lookup", "smalltalk", "unsupported"):
        assert intent in s


def test_user_with_history_includes_last_turns():
    hist = [("q1", "a1"), ("q2", "a2"), ("q3", "a3"), ("q4", "a4")]
    out = prompts.user_with_history("current?", hist)
    assert "current?" in out and "q4" in out and "q2" in out
    assert "q1" not in out  # only last 3 turns
