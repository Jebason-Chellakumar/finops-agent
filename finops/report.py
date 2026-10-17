"""Deterministic weekly FinOps report (Markdown). No LLM, no API key needed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from . import analysis as a
from .records import CostRecord


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _pct(p: float | None) -> str:
    return "new" if p is None else f"{p:+.1f}%"


def build_report(records: Sequence[CostRecord], as_of: date | None = None) -> str:
    _, last = a.date_range(records)
    end = as_of or last
    cur_start = end - timedelta(days=29)
    base_end = cur_start - timedelta(days=1)
    base_start = base_end - timedelta(days=29)

    forecast = a.forecast_month_end(records, end)
    summary = a.summarize(records, cur_start, end, ["service"], top_n=6)
    change = a.compare_periods(records, base_start, base_end, cur_start, end, ["service", "team"], top_n=5)
    detection = a.detect_anomalies(records, cur_start, end)
    anomalies = detection["anomalies"]
    idle = a.idle_candidates(records, end - timedelta(days=13), end)["candidates"]
    untagged = a.untagged_spend(records, cur_start, end)

    out = [
        f"# FinOps report: {end.isoformat()}",
        "",
        f"**Last 30 days:** {_usd(summary['total'])} ({_usd(summary['daily_average'])}/day, "
        f"{_pct(change['delta_pct'])} vs previous 30 days)  ",
        f"**Month-end forecast:** {_usd(forecast['forecast_month_total'])} "
        f"({_pct(forecast['forecast_vs_previous_pct'])} vs last month's "
        f"{_usd(forecast['previous_month_total'])})",
        "",
        "## Top services (last 30 days)",
        "| service | cost | share |",
        "|---|---:|---:|",
        *[f"| {g['service']} | {_usd(g['cost'])} | {g['share_pct']}% |" for g in summary["groups"]],
        "",
        "## Biggest changes (daily average, vs previous 30 days)",
        "| service | team | before/day | now/day | change per 30 days |",
        "|---|---|---:|---:|---:|",
        *[
            f"| {d['service']} | {d['team']} | {_usd(d['base_daily'])} | {_usd(d['current_daily'])} | "
            f"{_signed(d['delta_per_30_days'])} ({_pct(d['delta_pct'])}) |"
            for d in change["drivers"]
        ],
        "",
        "## Anomalies (last 30 days)",
    ]
    if anomalies:
        for an in anomalies:
            status = "**ongoing**" if an["ongoing"] else f"ended {an['end']}"
            days = f"{an['days']} day{'s' if an['days'] != 1 else ''}"
            out.append(
                f"- **{an['service']} / {an['team']}**: {_usd(an['expected_daily'])}/day → "
                f"{_usd(an['actual_daily'])}/day from {an['start']} ({days}, {status}), "
                f"excess {_usd(an['excess_total'])}"
            )
    else:
        out.append("- None detected.")

    out += ["", "## Savings opportunities"]
    for c in idle:
        out.append(
            f"- **Always-on {c['environment']}**: {c['service']} `{c['usage_type']}` ({c['team']}, "
            f"account {c['account']}) costs {_usd(c['monthly_cost'])}/month at a flat rate every day. "
            f"Scheduling to business hours saves ~{_usd(c['est_monthly_savings_if_scheduled'])}/month."
        )
    if untagged["untagged_cost"]:
        top = untagged["by_service"][0]["service"] if untagged["by_service"] else "n/a"
        out.append(
            f"- **Untagged spend**: {_usd(untagged['untagged_cost'])} ({untagged['share_pct']}%) in the "
            f"last 30 days has no `team` tag, mostly {top}. Enforce tags with an AWS Organizations tag policy."
        )
    if not idle and not untagged["untagged_cost"]:
        out.append("- None found.")
    out += [
        "",
        f"_Anomalies: {detection['method']}. Scheduling savings assume ~50 of 168 hours/week._",
    ]
    return "\n".join(out) + "\n"
