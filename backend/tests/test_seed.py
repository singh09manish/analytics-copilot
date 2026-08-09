import importlib.util
import random
from pathlib import Path

SEED_PATH = Path(__file__).parents[2] / "data" / "seed" / "generate.py"
spec = importlib.util.spec_from_file_location("seedgen", SEED_PATH)
seedgen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seedgen)


def _all():
    rng = random.Random(42)
    centers = seedgen.gen_centers(rng)
    machines = seedgen.gen_machines(rng, centers)
    util = seedgen.gen_utilization(rng, machines)
    tickets = seedgen.gen_tickets(rng, machines)
    return centers, machines, util, tickets


def test_row_counts_and_determinism():
    c1, m1, u1, t1 = _all()
    c2, m2, u2, t2 = _all()
    assert len(c1) == 63  # 60 + 3 duplicate rows
    assert len(m1) == 204  # 200 + 4 duplicate rows
    assert len(u1) == 146_000  # 200 machines × 730 days (all machines, not just active)
    assert 7000 < len(t1) < 9000
    assert c1 == c2 and m1[:50] == m2[:50] and u1[:50] == u2[:50] and t1[:50] == t2[:50]
    # Verify realistic active/decommissioned split
    active = sum(1 for m in m1[:200] if m["status"] == "Active")
    assert 120 <= active <= 180  # ~75% active rate with 200 machines


def test_mess_is_present():
    centers, machines, util, tickets = _all()
    assert any(c["contact_email"] == "" for c in centers)
    assert len({c["center_id"] for c in centers}) == 60
    assert any("/" in m["install_date"] for m in machines)  # mixed date formats
    assert any(r["uptime_hours"] == "" for r in util)
    severities = {t["severity"] for t in tickets}
    assert "critical" in severities and "Critical" in severities  # case mess
