"""Deterministic cost analytics.

Every number an agent reports comes from these functions, never from the model's own
arithmetic. All functions return plain JSON-serializable dicts so they double as tool
results and as the data behind the no-LLM report.
"""

from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from .records import DIMENSIONS, CostRecord

PROD_ENVIRONMENTS = {"prod", "production"}
WORKING_HOURS_SHARE = 50 / 168  # a non-prod resource scheduled for ~10h x 5 days


class AnalysisError(ValueError):
    """Bad input from a caller (surfaced to agents as a tool error)."""


# ---------------------------------------------------------------- helpers


def _days(start: date, end: date) -> list[date]:
    if end < start:
        raise AnalysisError(f"end {end} is before start {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _check_dims(group_by: Sequence[str]) -> tuple[str, ...]:
    bad = [d for d in group_by if d not in DIMENSIONS]
    if bad:
        raise AnalysisError(f"unknown dimension(s) {bad}; use {list(DIMENSIONS)}")
    return tuple(group_by)


def select(
    records: Iterable[CostRecord],
    start: date,
    end: date,
    filters: dict[str, str] | None = None,
) -> list[CostRecord]:
    filters = {k: v for k, v in (filters or {}).items() if v is not None}
    _check_dims(list(filters))
    return [r for r in records if start <= r.day <= end and all(getattr(r, k) == v for k, v in filters.items())]


def _label(key: tuple[str, ...], group_by: Sequence[str]) -> dict[str, str]:
    return {d: (v if v else "(none)") for d, v in zip(group_by, key)}


def _money(x: float) -> float:
    return round(x, 2)


def _pct(new: float, old: float) -> float | None:
    return round((new - old) / old * 100, 1) if old else None


def date_range(records: Sequence[CostRecord]) -> tuple[date, date]:
    if not records:
        raise AnalysisError("dataset is empty")
    days = [r.day for r in records]
    return min(days), max(days)


# ---------------------------------------------------------------- queries


def dataset_info(records: Sequence[CostRecord]) -> dict:
    first, last = date_range(records)
    values = {d: sorted({getattr(r, d) or "(none)" for r in records}) for d in DIMENSIONS}
    return {
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "days": (last - first).days + 1,
        "total_cost": _money(sum(r.cost for r in records)),
        "dimensions": {d: v for d, v in values.items() if v != ["(none)"]},
    }


def summarize(
    records: Sequence[CostRecord],
    start: date,
    end: date,
    group_by: Sequence[str],
    filters: dict[str, str] | None = None,
    top_n: int = 10,
) -> dict:
    group_by = _check_dims(group_by)
    rows = select(records, start, end, filters)
    totals: dict[tuple, float] = defaultdict(float)
    for r in rows:
        totals[tuple(getattr(r, d) for d in group_by)] += r.cost
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": _money(total),
        "daily_average": _money(total / len(_days(start, end))),
        "groups": [
            {**_label(k, group_by), "cost": _money(v), "share_pct": round(v / total * 100, 1) if total else 0}
            for k, v in ranked[:top_n]
        ],
        "other_groups": max(0, len(ranked) - top_n),
    }


def compare_periods(
    records: Sequence[CostRecord],
    base_start: date,
    base_end: date,
    current_start: date,
    current_end: date,
    group_by: Sequence[str],
    filters: dict[str, str] | None = None,
    top_n: int = 10,
) -> dict:
    """Biggest movers between two periods, compared on daily averages.

    Daily averages make unequal periods (e.g. a 31-day vs a 30-day month) comparable.
    """
    group_by = _check_dims(group_by)
    n_base, n_cur = len(_days(base_start, base_end)), len(_days(current_start, current_end))

    def daily(start, end, n):
        acc: dict[tuple, float] = defaultdict(float)
        for r in select(records, start, end, filters):
            acc[tuple(getattr(r, d) for d in group_by)] += r.cost / n
        return acc

    base, cur = daily(base_start, base_end, n_base), daily(current_start, current_end, n_cur)
    drivers = []
    for key in set(base) | set(cur):
        b, c = base.get(key, 0.0), cur.get(key, 0.0)
        drivers.append(
            {
                **_label(key, group_by),
                "base_daily": _money(b),
                "current_daily": _money(c),
                "delta_daily": _money(c - b),
                "delta_pct": _pct(c, b),
                "delta_per_30_days": _money((c - b) * 30),
            }
        )
    drivers.sort(key=lambda d: -abs(d["delta_daily"]))
    tb, tc = sum(base.values()), sum(cur.values())
    return {
        "base_period": f"{base_start.isoformat()}..{base_end.isoformat()}",
        "current_period": f"{current_start.isoformat()}..{current_end.isoformat()}",
        "base_daily_total": _money(tb),
        "current_daily_total": _money(tc),
        "delta_daily_total": _money(tc - tb),
        "delta_pct": _pct(tc, tb),
        "drivers": drivers[:top_n],
    }


def daily_series(
    records: Sequence[CostRecord],
    start: date,
    end: date,
    filters: dict[str, str] | None = None,
) -> dict:
    by_day: dict[date, float] = defaultdict(float)
    for r in select(records, start, end, filters):
        by_day[r.day] += r.cost
    series = [{"day": d.isoformat(), "cost": _money(by_day.get(d, 0.0))} for d in _days(start, end)]
    costs = [p["cost"] for p in series]
    return {
        "filters": {k: v for k, v in (filters or {}).items() if v is not None},
        "series": series,
        "min": min(costs),
        "max": max(costs),
        "mean": _money(statistics.fmean(costs)),
    }


def detect_anomalies(
    records: Sequence[CostRecord],
    start: date,
    end: date,
    group_by: Sequence[str] = ("service", "team"),
    lookback_days: int = 14,
    z_threshold: float = 3.0,
    min_daily_excess: float = 20.0,
    top_n: int = 10,
) -> dict:
    """Find spend spikes per series using a rolling baseline.

    A day is anomalous when cost > mean + z * spread of the previous ``lookback_days``,
    where spread is the standard deviation floored at 10% of the mean (so very stable
    series don't alert on noise) and the excess is at least ``min_daily_excess`` dollars.
    The baseline is frozen while an episode lasts, so a sustained step change stays
    flagged instead of being absorbed into its own baseline.
    """
    group_by = _check_dims(group_by)
    history_start = start - timedelta(days=lookback_days)
    days = _days(history_start, end)
    series: dict[tuple, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for r in select(records, history_start, end):
        series[tuple(getattr(r, d) for d in group_by)][r.day] += r.cost

    episodes = []
    for key, by_day in series.items():
        values = [by_day.get(d, 0.0) for d in days]
        current = None
        for i in range(lookback_days, len(days)):
            if current is None:
                window = values[i - lookback_days : i]
                mean = statistics.fmean(window)
                spread = max(statistics.pstdev(window), 0.1 * mean, 1.0)
            else:
                mean, spread = current["_mean"], current["_spread"]
            v = values[i]
            # Hysteresis: a big jump opens an episode; it closes only once spend is back
            # near baseline (so e.g. a quieter weekend doesn't split one incident in two).
            if current is None:
                anomalous = v > mean + z_threshold * spread and v - mean >= min_daily_excess
            else:
                anomalous = v > mean + spread and v - mean >= min_daily_excess / 2
            if anomalous:
                if current is None:
                    current = {"_mean": mean, "_spread": spread, "start": days[i], "values": []}
                current["values"].append(v)
                current["end"] = days[i]
            elif current is not None:
                episodes.append((key, current))
                current = None
        if current is not None:
            episodes.append((key, current))

    out = []
    for key, ep in episodes:
        n = len(ep["values"])
        actual = statistics.fmean(ep["values"])
        out.append(
            {
                **_label(key, group_by),
                "start": ep["start"].isoformat(),
                "end": ep["end"].isoformat(),
                "days": n,
                "ongoing": ep["end"] == end,
                "expected_daily": _money(ep["_mean"]),
                "actual_daily": _money(actual),
                "excess_total": _money(sum(ep["values"]) - ep["_mean"] * n),
                "increase_pct": _pct(actual, ep["_mean"]),
            }
        )
    out.sort(key=lambda a: -a["excess_total"])
    return {
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "method": f"rolling {lookback_days}-day baseline, z>{z_threshold}, excess>=${min_daily_excess}/day",
        "anomalies": out[:top_n],
    }


def forecast_month_end(records: Sequence[CostRecord], as_of: date) -> dict:
    month_start = as_of.replace(day=1)
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)

    def total(s, e):
        return sum(r.cost for r in select(records, s, e))

    mtd = total(month_start, as_of)
    trailing = total(as_of - timedelta(days=6), as_of) / 7
    forecast = mtd + trailing * (days_in_month - as_of.day)
    previous = total(prev_start, prev_end)
    return {
        "as_of": as_of.isoformat(),
        "month_to_date": _money(mtd),
        "run_rate_daily": _money(trailing),
        "forecast_month_total": _money(forecast),
        "previous_month_total": _money(previous),
        "forecast_vs_previous_pct": _pct(forecast, previous),
        "method": "month-to-date + trailing 7-day average x remaining days",
    }


def untagged_spend(records: Sequence[CostRecord], start: date, end: date, tag: str = "team") -> dict:
    _check_dims([tag])
    rows = select(records, start, end)
    total = sum(r.cost for r in rows)
    untagged = [r for r in rows if not getattr(r, tag)]
    by_service: dict[str, float] = defaultdict(float)
    for r in untagged:
        by_service[r.service] += r.cost
    cost = sum(by_service.values())
    return {
        "tag": tag,
        "untagged_cost": _money(cost),
        "share_pct": round(cost / total * 100, 1) if total else 0,
        "by_service": [
            {"service": s, "cost": _money(c)} for s, c in sorted(by_service.items(), key=lambda kv: -kv[1])
        ],
    }


def idle_candidates(
    records: Sequence[CostRecord],
    start: date,
    end: date,
    min_daily_cost: float = 10.0,
    max_variation_pct: float = 3.0,
) -> dict:
    """Non-production resources whose cost is flat every day, weekends included.

    Flat 24/7 cost in dev/staging usually means an always-on resource nobody uses
    outside working hours: a scheduling or shutdown candidate.
    """
    days = _days(start, end)
    series: dict[tuple, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    keys = ("service", "team", "environment", "usage_type", "account")
    for r in select(records, start, end):
        if r.environment and r.environment.lower() not in PROD_ENVIRONMENTS:
            series[tuple(getattr(r, k) for k in keys)][r.day] += r.cost

    out = []
    for key, by_day in series.items():
        values = [by_day.get(d, 0.0) for d in days]
        running = [v for v in values if v > 0]
        mean = statistics.fmean(running) if running else 0.0
        if len(running) < 7 or mean < min_daily_cost:
            continue
        variation = statistics.pstdev(running) / mean * 100
        if variation > max_variation_pct:
            continue
        monthly = mean * 30
        out.append(
            {
                **_label(key, keys),
                "days_running": len(running),
                "daily_cost": _money(mean),
                "variation_pct": round(variation, 2),
                "monthly_cost": _money(monthly),
                "est_monthly_savings_if_scheduled": _money(monthly * (1 - WORKING_HOURS_SHARE)),
            }
        )
    out.sort(key=lambda c: -c["monthly_cost"])
    return {
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "assumption": "savings assume running ~50 of 168 hours/week (business hours only)",
        "candidates": out,
    }
