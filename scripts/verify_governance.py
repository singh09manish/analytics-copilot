"""Same query, two roles — proves masking + RBAC. Also proves RO cannot see SILVER."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))
from copilot.snowflake_client import SnowflakeClient

Q = "SELECT center_id, contact_email FROM MEDTECH_ANALYTICS.GOLD.DIM_TREATMENT_CENTER LIMIT 3"

ro = SnowflakeClient(role="COPILOT_APP_RO")
admin = SnowflakeClient(role="COPILOT_ADMIN")
ro_rows = ro.run_query(Q)[1]
admin_rows = admin.run_query(Q)[1]
print("RO   :", ro_rows)
print("ADMIN:", admin_rows)

# Verify RO is blocked from SILVER with correct authorization error.
try:
    ro.run_query("SELECT COUNT(*) FROM MEDTECH_ANALYTICS.SILVER.CENTERS")
    print("FAIL: RO could read SILVER")
    sys.exit(1)
except Exception as e:  # noqa: BLE001
    error_msg = str(e).lower()
    if "not authorized" not in error_msg and "002003" not in error_msg:
        print(f"FAIL: SILVER probe failed for wrong reason: {e}")
        sys.exit(1)
    print("OK: RO blocked from SILVER")

# Verify secondary roles are pinned to NONE for RO.
ro_secondary = ro.run_query("SELECT CURRENT_SECONDARY_ROLES()")[1]
print("RO SECONDARY_ROLES:", ro_secondary)
if ro_secondary and ro_secondary[0] and ro_secondary[0][0]:
    # Extract the value from the response (may be a tuple or dict)
    secondary_val = str(ro_secondary[0][0])
    if (
        "roles" in secondary_val.lower()
        and "value" in secondary_val.lower()
        and '""' not in secondary_val
        and "none" not in secondary_val.lower()
    ):
        # It's still showing secondary roles info with non-empty roles
        print("FAIL: RO has non-empty secondary roles")
        sys.exit(1)
print("OK: RO secondary roles pinned")

# Verify masking works: RO sees masked, ADMIN sees unmasked.
if not ro_rows:
    print("FAIL: RO query returned no rows")
    sys.exit(1)
# Strict on purpose: the masking policy's CASE branches on current_role(), not on
# whether contact_email is NULL, so a working RO view *always* returns the literal
# "***MASKED***" token -- never NULL, even for the ~7 of 60 centres that genuinely
# have no email on file (those NULLs only ever surface on the ADMIN side above).
# A NULL here would mean the view fell through to the raw column (masking broken),
# and 7/60 centres would coincidentally make that failure look like a pass if NULL
# were accepted as "masked". So this must be an exact match, not `in (None, ...)`.
masked = all(r[1] == "***MASKED***" for r in ro_rows)
clear = any(r[1] and "@" in r[1] for r in admin_rows)
print("MASKING OK" if masked and clear else "MASKING BROKEN")
sys.exit(0 if masked and clear else 1)
