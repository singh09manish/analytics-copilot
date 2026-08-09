"""Deterministic synthetic med-device analytics data with realistic mess."""
import csv
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "out"
END = date(2026, 8, 7)
DAYS = 730
MODELS = ["TrueBeam", "TrueBeam STx", "Halcyon", "Ethos", "Clinac iX", "ProBeam"]
REGIONS = ["NA", "EMEA", "APAC", "LATAM"]
CITIES = {
    "NA": [("Atlanta", "USA"), ("Orlando", "USA"), ("Toronto", "Canada"), ("Chicago", "USA"),
           ("Houston", "USA"), ("Phoenix", "USA")],
    "EMEA": [("Berlin", "Germany"), ("London", "UK"), ("Madrid", "Spain"), ("Lagos", "Nigeria")],
    "APAC": [("Tokyo", "Japan"), ("Sydney", "Australia"), ("Mumbai", "India"), ("Seoul", "Korea")],
    "LATAM": [("Sao Paulo", "Brazil"), ("Mexico City", "Mexico"), ("Bogota", "Colombia")],
}
NAME_A = ["Northside", "Mercy", "St. Luke", "Riverview", "Summit", "Lakeside", "Unity",
          "Providence", "Horizon", "Beacon", "Cedar", "Evergreen"]
NAME_B = ["Oncology Center", "Cancer Institute", "Radiotherapy Clinic", "Medical Center",
          "Regional Hospital"]
DOWNTIME_REASONS = ["Beam fault", "Imaging fault", "Software error", "Cooling system",
                    "Scheduled maintenance", "MLC fault", "Power interruption"]
CATEGORIES = ["Beam Generation", "Imaging", "Software", "Cooling", "Mechanical", "Electrical"]
SEVERITIES = ["Critical", "High", "Medium", "Low"]


def gen_centers(rng: random.Random) -> list[dict]:
    rows = []
    for i in range(60):
        region = REGIONS[i % 4] if rng.random() > 0.2 else REGIONS[i % 4].lower()
        city, country = rng.choice(CITIES[REGIONS[i % 4]])
        name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B)} {i:02d}"
        email = "" if rng.random() < 0.08 else f"ops{i:02d}@{name.split()[0].lower().replace('.', '')}health.org"
        rows.append({
            "center_id": f"C{i:03d}", "center_name": name, "region": region,
            "country": country, "city": city, "beds": rng.randint(40, 900),
            "contact_email": email,
            "go_live_date": (END - timedelta(days=rng.randint(400, 4000))).isoformat(),
        })
    rows += [dict(rows[i]) for i in (3, 17, 42)]  # duplicate rows (mess)
    return rows


def gen_machines(rng: random.Random, centers: list[dict]) -> list[dict]:
    ids = sorted({c["center_id"] for c in centers})
    rows = []
    for i in range(200):
        d = END - timedelta(days=rng.randint(200, 3500))
        fmt = d.isoformat() if rng.random() < 0.7 else d.strftime("%m/%d/%Y")
        rows.append({
            "machine_id": f"M{i:04d}", "center_id": rng.choice(ids),
            "model": rng.choice(MODELS), "serial": f"SN-{rng.randint(100000, 999999)}",
            "install_date": fmt, "sw_version": f"{rng.randint(15, 18)}.{rng.randint(0, 9)}",
            "status": rng.choices(["Active", "Active", "Active", "Decommissioned"])[0],
        })
    rows += [dict(rows[i]) for i in (5, 50, 111, 180)]
    return rows


def gen_utilization(rng: random.Random, machines: list[dict]) -> list[dict]:
    fleet = machines[:200]
    rows = []
    for m in fleet:
        base = rng.uniform(0.88, 0.99)  # per-machine reliability
        for d in range(DAYS):
            day = END - timedelta(days=DAYS - 1 - d)
            planned = rng.randint(20, 42) if day.weekday() < 5 else rng.randint(0, 8)
            down = 0.0
            reason = ""
            if rng.random() > base:
                down = round(rng.uniform(0.5, 14.0), 1)
                reason = rng.choice(DOWNTIME_REASONS)
            uptime = round(24.0 - down, 1)
            lost = min(planned, int(down * 2.2))
            row = {
                "machine_id": m["machine_id"],
                "log_date": day.isoformat() if rng.random() < 0.85 else day.strftime("%m/%d/%Y"),
                "planned_fractions": planned, "delivered_fractions": max(planned - lost, 0),
                "uptime_hours": uptime, "downtime_hours": down, "downtime_reason": reason,
            }
            if rng.random() < 0.01:
                row["uptime_hours"] = ""  # mess: missing telemetry
            rows.append(row)
    return rows


def gen_tickets(rng: random.Random, machines: list[dict]) -> list[dict]:
    active = [m for m in machines[:200]]
    rows = []
    for i in range(8000):
        m = rng.choice(active)
        opened = datetime(2024, 8, 8, tzinfo=UTC) + timedelta(hours=rng.randint(0, DAYS * 24 - 48))
        sev = rng.choices(SEVERITIES, weights=[1, 3, 6, 5])[0]
        if rng.random() < 0.15:
            sev = sev.lower()  # mess: inconsistent case
        res_hours = round(rng.uniform(0.5, 200 if "c" in sev.lower() else 400), 1)
        closed = opened + timedelta(hours=res_hours)
        rows.append({
            "ticket_id": f"T{i:05d}", "machine_id": m["machine_id"],
            "opened_at": opened.isoformat(sep=" "),
            "closed_at": "" if rng.random() < 0.06 else closed.isoformat(sep=" "),
            "severity": sev, "category": rng.choice(CATEGORIES),
            "resolution_hours": res_hours,
            "parts_cost": round(rng.uniform(0, 25000), 2) if rng.random() < 0.5 else 0.0,
        })
    return rows


def _write(name: str, rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    rng = random.Random(42)
    centers = gen_centers(rng)
    machines = gen_machines(rng, centers)
    _write("centers.csv", centers)
    _write("machines.csv", machines)
    _write("utilization.csv", gen_utilization(rng, machines))
    _write("service_tickets.csv", gen_tickets(rng, machines))
    print(f"wrote 4 CSVs to {OUT}")


if __name__ == "__main__":
    main()
