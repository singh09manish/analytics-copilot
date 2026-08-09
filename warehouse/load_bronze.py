"""Land seed CSVs into BRONZE as-is (all VARCHAR). Idempotent full reload."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))
from copilot.snowflake_client import SnowflakeClient

SEED = Path(__file__).parents[1] / "data" / "seed" / "out"
TABLES = {
    "RAW_CENTERS": "centers.csv",
    "RAW_MACHINES": "machines.csv",
    "RAW_UTILIZATION": "utilization.csv",
    "RAW_SERVICE_TICKETS": "service_tickets.csv",
}


def main() -> None:
    sf = SnowflakeClient(role="COPILOT_ADMIN")

    # Try to use the configured SEED_STAGE, fall back to creating our own if not accessible
    stage_path = "@MEDTECH_ANALYTICS.BRONZE.SEED_STAGE"
    try:
        # Test if SEED_STAGE is accessible
        sf.run_query(f"LIST {stage_path}")
    except Exception:  # noqa: BLE001
        # SEED_STAGE not accessible, create our own
        stage_path = "@MEDTECH_ANALYTICS.BRONZE.LOAD_STAGE"
        try:
            sf.run_query("CREATE STAGE IF NOT EXISTS MEDTECH_ANALYTICS.BRONZE.LOAD_STAGE")
        except Exception:  # noqa: BLE001, S110
            pass  # Might already exist

    for table, fname in TABLES.items():
        path = SEED / fname
        with open(path) as f:
            headers = next(csv.reader(f))
        cols = ", ".join(f"{h.upper()} VARCHAR" for h in headers)
        fq = f"MEDTECH_ANALYTICS.BRONZE.{table}"
        sf.execute_many([
            f"CREATE OR REPLACE TABLE {fq} ({cols}, _LOADED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP())",
            f"PUT file://{path.resolve()} {stage_path}/{table}/ OVERWRITE=TRUE AUTO_COMPRESS=TRUE",
        ])
        col_list = ", ".join(h.upper() for h in headers)
        sel = ", ".join(f"${i + 1}" for i in range(len(headers)))
        sf.run_query(
            f"COPY INTO {fq} ({col_list}) FROM (SELECT {sel} FROM "
            f"{stage_path}/{table}/) "
            f"FILE_FORMAT=(TYPE=CSV SKIP_HEADER=1 FIELD_OPTIONALLY_ENCLOSED_BY='\"' EMPTY_FIELD_AS_NULL=TRUE) PURGE=TRUE"
        )
        _, n = sf.run_query(f"SELECT COUNT(*) FROM {fq}")
        print(f"{table}: {n[0][0]} rows")


if __name__ == "__main__":
    main()
