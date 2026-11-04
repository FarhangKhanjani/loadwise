"""Training load metrics. Pure functions: no database, no LLM, fully unit tested.

Daily loads are a dict {date: load}. A date missing from the dict is a rest day (load 0),
so callers never have to fill in zeros.

Definitions (session-RPE method, Foster et al.):
  session load = duration_min x RPE                      (arbitrary units, AU)
  daily load   = sum of session loads on that day
  acute load   = sum of daily load over the last 7 days  (as_of included)
  chronic load = sum of daily load over the last 28 days / 4   (average week)
  ACWR         = acute / chronic          (coupled: the acute week is inside the 28 days)
  monotony     = mean daily load / SD of daily load over the last 7 days
  strain       = weekly (acute) load x monotony
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Protocol

ACUTE_DAYS = 7
CHRONIC_DAYS = 28


class SessionLike(Protocol):
    """Anything with these attributes works: ORM rows, dataclasses, namedtuples."""

    date: date
    duration_min: int
    rpe: int


def session_load(duration_min: int, rpe: int) -> int:
    return duration_min * rpe


def daily_loads(sessions: Iterable[SessionLike]) -> dict[date, int]:
    loads: dict[date, int] = defaultdict(int)
    for s in sessions:
        loads[s.date] += session_load(s.duration_min, s.rpe)
    return dict(loads)


def _window(daily: dict[date, int], as_of: date, days: int) -> list[int]:
    """Daily loads for the `days` days ending on as_of (inclusive), oldest first."""
    return [daily.get(as_of - timedelta(days=offset), 0) for offset in range(days - 1, -1, -1)]


def acute_load(daily: dict[date, int], as_of: date) -> int:
    return sum(_window(daily, as_of, ACUTE_DAYS))


def chronic_load(daily: dict[date, int], as_of: date) -> float:
    """Average weekly load over the last 28 days."""
    return sum(_window(daily, as_of, CHRONIC_DAYS)) / 4


def acwr(daily: dict[date, int], as_of: date, history_start: date) -> float | None:
    """Acute:chronic workload ratio.

    None when there are fewer than 28 days of history (as_of included), because the
    chronic window would contain days before the athlete started logging, which would
    look like rest and inflate the ratio. Also None when chronic load is 0 (no training
    for 28 days), where the ratio is undefined.
    """
    days_of_history = (as_of - history_start).days + 1
    if days_of_history < CHRONIC_DAYS:
        return None
    chronic = chronic_load(daily, as_of)
    if chronic == 0:
        return None
    return acute_load(daily, as_of) / chronic


def monotony(daily: dict[date, int], as_of: date) -> float | None:
    """Mean / SD of daily load over the last 7 days.

    Uses the population SD (ddof=0): the 7 days are the whole week being described,
    not a sample of something larger. (The sample SD would make monotony about 7% lower.)
    None when SD is 0 (every day identical, including a week of only rest days):
    the ratio is undefined there.
    """
    week = _window(daily, as_of, ACUTE_DAYS)
    sd = statistics.pstdev(week)
    if sd == 0:
        return None
    return statistics.mean(week) / sd


def strain(daily: dict[date, int], as_of: date) -> float | None:
    m = monotony(daily, as_of)
    if m is None:
        return None
    return acute_load(daily, as_of) * m


@dataclass(frozen=True)
class LoadMetrics:
    as_of: date
    acute: int
    chronic: float
    acwr: float | None
    monotony: float | None
    strain: float | None


def compute_load_metrics(daily: dict[date, int], as_of: date, history_start: date) -> LoadMetrics:
    return LoadMetrics(
        as_of=as_of,
        acute=acute_load(daily, as_of),
        chronic=chronic_load(daily, as_of),
        acwr=acwr(daily, as_of, history_start),
        monotony=monotony(daily, as_of),
        strain=strain(daily, as_of),
    )
