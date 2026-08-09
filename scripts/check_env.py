"""Verify all three credentials work before building anything."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))


def main() -> int:
    ok = True

    for var in ("ANTHROPIC_API_KEY", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"):
        if not os.environ.get(var):
            print(f"MISSING env: {var}")
            ok = False
    try:
        import anthropic

        client = anthropic.Anthropic()
        r = client.messages.create(
            model=os.environ.get("APP_MODEL", "claude-sonnet-5"),
            max_tokens=16, messages=[{"role": "user", "content": "say ok"}],
        )
        print(f"anthropic OK: {r.content[0].text[:20]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"anthropic FAIL: {e}")
        ok = False
    try:
        from copilot.snowflake_client import SnowflakeClient

        sf = SnowflakeClient(role="COPILOT_ADMIN")
        _cols, rows = sf.run_query("SELECT CURRENT_ROLE(), CURRENT_ACCOUNT()")
        print(f"snowflake OK: {rows[0]}")
    except Exception as e:  # noqa: BLE001
        print(f"snowflake FAIL: {e}")
        ok = False
    print("ALL GOOD" if ok else "FIX THE ABOVE FIRST")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
