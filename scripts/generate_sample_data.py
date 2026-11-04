"""Generate 60 days of realistic synthetic training and wellness data.

Deterministic: fixed seed and fixed dates, so tests and demos always see the same data.

Timeline (2026-01-05 Mon .. 2026-03-05 Thu, 60 days):
  Weeks 1-5  normal training, ~5 sessions/week, slow build (~1900 -> ~2100 load units/week)
  Week 6     overload spike (Feb 9-15): no rest days except one, longer and harder sessions,
             ~2.1x a normal week. With the coupled ACWR formula this gives ACWR ~1.6.
             Wellness reacts: short sleep, soreness >= 7 on consecutive days, higher resting HR.
  Weeks 7-9  recovery block, then back to normal.

Why ~2.1x and not 1.6x? Acute load is also part of the 28-day chronic window, so
ACWR = 4A / (A + P), where P is the previous 3 weeks. To clear 1.5 the spike week
must be about 1.8x a normal week. We aim a bit higher so the flag is unambiguous.

Usage:
  python scripts/generate_sample_data.py [--out-dir data]
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
START = date(2026, 1, 5)  # a Monday, so weeks line up with Mon-Sun
DAYS = 60
SPIKE_WEEK = 6  # 1-based

# Normal week: weekday (0=Mon) -> (sport, duration_min, rpe, note)
NORMAL_WEEK = {
    0: ("running", 50, 6, "easy-moderate run"),
    1: ("strength", 60, 6, "full body"),
    3: ("running", 45, 8, "intervals"),
    5: ("cycling", 90, 5, "endurance ride"),
    6: ("running", 70, 6, "long run"),
}

# Spike week: more sessions, longer and harder. Wednesday stays the only rest day.
# Some days have two sessions.
SPIKE_WEEK_PLAN = {
    0: [("running", 70, 7, "tempo run")],
    1: [("strength", 75, 8, "heavy lower body"), ("cycling", 45, 5, "spin")],
    3: [("running", 60, 9, "hard intervals")],
    4: [("cycling", 120, 6, "long ride")],
    5: [("running", 90, 7, "long tempo"), ("strength", 45, 7, "upper body")],
    6: [("running", 110, 7, "very long run, heavy legs")],
}

# Load multiplier per week (applied to duration). Weeks 1-5 build slowly,
# week 7 is a deload after the spike, weeks 8-9 return to normal.
WEEK_FACTOR = {1: 0.95, 2: 0.98, 3: 1.0, 4: 1.03, 5: 1.06, 7: 0.7, 8: 0.95, 9: 1.0}


def week_of(day_index: int) -> int:
    return day_index // 7 + 1


def generate_sessions(rng: random.Random) -> list[dict]:
    rows = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        week = week_of(i)
        weekday = d.weekday()

        if week == SPIKE_WEEK:
            plan = SPIKE_WEEK_PLAN.get(weekday, [])
            for sport, duration, rpe, note in plan:
                rows.append(_session(d, sport, duration + rng.randint(-5, 5), rpe, note))
            continue

        if weekday not in NORMAL_WEEK:
            continue  # rest day
        sport, duration, rpe, note = NORMAL_WEEK[weekday]
        duration = round(duration * WEEK_FACTOR[week] * rng.uniform(0.92, 1.08))
        rpe = max(1, min(10, rpe + rng.choice([-1, 0, 0, 0, 1])))
        rows.append(_session(d, sport, duration, rpe, note))
    return rows


def _session(d: date, sport: str, duration: int, rpe: int, note: str) -> dict:
    return {"date": d.isoformat(), "sport_type": sport, "duration_min": duration, "rpe": rpe, "notes": note}


def generate_wellness(rng: random.Random) -> list[dict]:
    # A few forgotten days, like a real athlete. Never in the spike week, so the
    # sleep and soreness flags there are unambiguous.
    candidates = [i for i in range(DAYS) if week_of(i) != SPIKE_WEEK]
    missing = set(rng.sample(candidates, 3))

    rows = []
    for i in range(DAYS):
        if i in missing:
            continue
        d = START + timedelta(days=i)
        week = week_of(i)
        day_in_week = i % 7

        if week == SPIKE_WEEK:
            # Gets worse as the week goes on.
            sleep = round(rng.uniform(5.0, 6.0) if day_in_week >= 2 else rng.uniform(6.2, 7.0), 1)
            soreness = 7 + (1 if day_in_week >= 4 else 0) if day_in_week >= 2 else rng.randint(5, 6)
            fatigue = rng.randint(7, 9)
            stress = rng.randint(5, 7)
            resting_hr = 52 + rng.randint(5, 8)
            note = "legs very heavy" if day_in_week >= 4 else ""
        elif week == SPIKE_WEEK + 1 and day_in_week < 3:
            # First days after the spike: still tired.
            sleep = round(rng.uniform(6.5, 7.5), 1)
            soreness = rng.randint(4, 6)
            fatigue = rng.randint(5, 6)
            stress = rng.randint(3, 5)
            resting_hr = 52 + rng.randint(2, 4)
            note = ""
        else:
            sleep = round(rng.uniform(6.8, 8.5), 1)
            soreness = rng.randint(2, 4)
            fatigue = rng.randint(2, 4)
            stress = rng.randint(2, 5)
            resting_hr = 52 + rng.randint(-2, 2)
            note = ""

        rows.append({
            "date": d.isoformat(),
            "sleep_hours": sleep,
            "soreness": soreness,
            "fatigue": fatigue,
            "stress": stress,
            "resting_hr": resting_hr,
            "notes": note,
        })
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    rng = random.Random(SEED)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    sessions = generate_sessions(rng)
    wellness = generate_wellness(rng)
    write_csv(out / "sample_sessions.csv", sessions)
    write_csv(out / "sample_wellness.csv", wellness)
    print(f"Wrote {len(sessions)} sessions and {len(wellness)} wellness rows to {out}/")


if __name__ == "__main__":
    main()
