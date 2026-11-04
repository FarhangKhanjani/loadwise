import math
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from src.metrics import (
    acute_load,
    acwr,
    chronic_load,
    compute_load_metrics,
    daily_loads,
    monotony,
    session_load,
    strain,
)

AS_OF = date(2026, 2, 28)


@dataclass
class S:
    date: date
    duration_min: int
    rpe: int


def days_before(n: int) -> date:
    return AS_OF - timedelta(days=n)


def week(loads: list[int]) -> dict[date, int]:
    """7 daily loads, oldest first, ending on AS_OF."""
    assert len(loads) == 7
    return {days_before(6 - i): load for i, load in enumerate(loads)}


# ---------- session and daily load ----------

def test_session_load_is_duration_times_rpe():
    assert session_load(60, 7) == 420


def test_daily_loads_sums_sessions_on_the_same_day():
    sessions = [S(AS_OF, 60, 7), S(AS_OF, 30, 4), S(days_before(1), 45, 6)]
    assert daily_loads(sessions) == {AS_OF: 420 + 120, days_before(1): 270}


def test_daily_loads_of_no_sessions_is_empty():
    assert daily_loads([]) == {}


# ---------- acute and chronic ----------

def test_acute_load_sums_the_last_7_days_including_as_of():
    daily = {AS_OF: 100, days_before(6): 50, days_before(7): 999}  # day 7 back is outside
    assert acute_load(daily, AS_OF) == 150


def test_acute_load_with_only_rest_days_is_zero():
    assert acute_load({}, AS_OF) == 0


def test_chronic_load_is_28_day_sum_divided_by_4():
    daily = {days_before(i): 100 for i in range(28)}  # 100 every day
    daily[days_before(28)] = 999                        # outside the window
    assert chronic_load(daily, AS_OF) == 28 * 100 / 4  # 700 per week


# ---------- ACWR ----------

def steady_then_spike() -> dict[date, int]:
    """3 weeks of 200/day (1400/week), then a week of 400/day (2800)."""
    daily = {days_before(i): 400 for i in range(7)}
    daily.update({days_before(i): 200 for i in range(7, 28)})
    return daily


def test_acwr_matches_hand_calculation():
    # acute 2800, chronic (3 * 1400 + 2800) / 4 = 1750, ACWR = 1.6
    assert acwr(steady_then_spike(), AS_OF, history_start=days_before(27)) == pytest.approx(1.6)


def test_acwr_equals_coupled_formula_4a_over_a_plus_p():
    a, p = 2800, 3 * 1400
    assert acwr(steady_then_spike(), AS_OF, days_before(27)) == pytest.approx(4 * a / (a + p))


def test_acwr_is_none_with_27_days_of_history():
    assert acwr(steady_then_spike(), AS_OF, history_start=days_before(26)) is None


def test_acwr_counts_rest_days_inside_history():
    # 28 days of history, but only the last week has training: ACWR = 4A / A = 4 (the coupled maximum)
    daily = {days_before(i): 300 for i in range(7)}
    assert acwr(daily, AS_OF, history_start=days_before(27)) == pytest.approx(4.0)


def test_acwr_is_none_when_chronic_load_is_zero():
    assert acwr({}, AS_OF, history_start=days_before(100)) is None


def test_acwr_steady_load_is_one():
    daily = {days_before(i): 250 for i in range(28)}
    assert acwr(daily, AS_OF, days_before(27)) == pytest.approx(1.0)


# ---------- monotony and strain ----------

def test_monotony_hand_calculated_single_session_week():
    # mean 100, population SD sqrt(6 * 100^2 + 600^2) / 7) = sqrt(60000), monotony = 1/sqrt(6)
    assert monotony(week([0, 0, 0, 0, 0, 0, 700]), AS_OF) == pytest.approx(1 / math.sqrt(6))


def test_monotony_uses_population_sd():
    loads = [400, 500, 400, 500, 400, 500, 450]
    mean = sum(loads) / 7
    population_sd = math.sqrt(sum((x - mean) ** 2 for x in loads) / 7)  # divide by n, not n-1
    assert monotony(week(loads), AS_OF) == pytest.approx(mean / population_sd)


def test_monotony_is_none_when_every_day_is_identical():
    assert monotony(week([300] * 7), AS_OF) is None


def test_monotony_is_none_for_a_week_of_rest():
    assert monotony({}, AS_OF) is None


def test_strain_is_weekly_load_times_monotony():
    daily = week([0, 0, 0, 0, 0, 0, 700])
    assert strain(daily, AS_OF) == pytest.approx(700 / math.sqrt(6))


def test_strain_is_none_when_monotony_is_none():
    assert strain(week([300] * 7), AS_OF) is None


# ---------- all together ----------

def test_compute_load_metrics_bundles_every_metric():
    daily = steady_then_spike()
    m = compute_load_metrics(daily, AS_OF, history_start=days_before(27))
    assert m.as_of == AS_OF
    assert m.acute == 2800
    assert m.chronic == 1750
    assert m.acwr == pytest.approx(1.6)
    assert m.monotony is None  # 400 every day in the last week: SD 0
    assert m.strain is None
