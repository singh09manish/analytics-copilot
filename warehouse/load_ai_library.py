"""Create + populate COPILOT.GLOSSARY and COPILOT.SCHEMA_CARDS with Cortex embeddings."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))
from copilot.snowflake_client import SnowflakeClient

LIB = Path(__file__).parents[1] / "data" / "ai_library"
EMBED = "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', %s)"


def main() -> None:
    sf = SnowflakeClient(role="COPILOT_ADMIN")
    sf.execute_many([
        (
            "CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.GLOSSARY ("
            "term VARCHAR, definition VARCHAR, related_tables VARCHAR, embedding VECTOR(FLOAT, 768))"
        ),
        (
            "CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS ("
            "table_name VARCHAR, card VARCHAR, embedding VECTOR(FLOAT, 768))"
        ),
    ])
    glossary = yaml.safe_load((LIB / "glossary.yaml").read_text())
    for g in glossary:
        text = f"{g['term']}: {g['definition']}"
        sf.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.GLOSSARY "
            f"SELECT %s, %s, %s, {EMBED}",
            (g["term"], g["definition"], ",".join(g["related_tables"]), text),
        )
    cards = yaml.safe_load((LIB / "schema_cards.yaml").read_text())
    for c in cards:
        sf.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS "
            f"SELECT %s, %s, {EMBED}",
            (c["table_name"], c["card"], c["card"]),
        )
    for t in ("GLOSSARY", "SCHEMA_CARDS"):
        _, n = sf.run_query(f"SELECT COUNT(*) FROM MEDTECH_ANALYTICS.COPILOT.{t}")
        print(f"{t}: {n[0][0]} rows")


if __name__ == "__main__":
    main()
