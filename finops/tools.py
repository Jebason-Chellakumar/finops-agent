"""Cost-data tools exposed to the agents.

Each tool is a strict JSON schema (``strict: true``: inputs always match the schema)
plus a handler over the analysis functions. Optional inputs are nullable fields so
every property can stay ``required``, as strict mode needs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import analysis as a
from .records import DIMENSIONS, CostRecord


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


# ---------------------------------------------------------------- schema helpers

DATE = {"type": "string", "description": "ISO date YYYY-MM-DD (inclusive)"}
GROUP_BY = {
    "type": "array",
    "items": {"type": "string", "enum": list(DIMENSIONS)},
    "description": 'Dimensions to group by, e.g. ["service", "team"]',
}
FILTERS = {
    "type": "object",
    "description": "Exact-match filters; null means no filter on that dimension.",
    "properties": {d: {"type": ["string", "null"]} for d in DIMENSIONS},
    "required": list(DIMENSIONS),
    "additionalProperties": False,
}


def _schema(**props) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise a.AnalysisError(f"{field} must be YYYY-MM-DD, got {value!r}") from None


def _filters(raw) -> dict[str, str]:
    raw = raw or {}
    return {k: ("" if v == "(none)" else v) for k, v in raw.items() if v is not None}


# ---------------------------------------------------------------- tool set


def build_tools(records: Sequence[CostRecord]) -> dict[str, Tool]:
    records = list(records)
    tools: list[Tool] = [
        Tool(
            "get_dataset_info",
            "Date range, total and the distinct values of every dimension (services, teams, "
            "environments, accounts, regions, usage types). Call this first to learn valid "
            "dates and filter values. '(none)' means the record has no value, e.g. untagged.",
            _schema(),
            lambda i: a.dataset_info(records),
        ),
        Tool(
            "summarize_costs",
            "Total cost for a date range, broken down by the given dimensions, largest first, "
            "with each group's share of the total.",
            _schema(start=DATE, end=DATE, group_by=GROUP_BY, filters=FILTERS),
            lambda i: a.summarize(
                records,
                _date(i["start"], "start"),
                _date(i["end"], "end"),
                i["group_by"],
                _filters(i["filters"]),
            ),
        ),
        Tool(
            "compare_periods",
            "Explain what changed between two periods: biggest movers by the given dimensions, "
            "compared on daily averages, with the change projected per 30 days. Use it for "
            "'why did the bill go up' questions.",
            _schema(
                base_start=DATE,
                base_end=DATE,
                current_start=DATE,
                current_end=DATE,
                group_by=GROUP_BY,
                filters=FILTERS,
            ),
            lambda i: a.compare_periods(
                records,
                _date(i["base_start"], "base_start"),
                _date(i["base_end"], "base_end"),
                _date(i["current_start"], "current_start"),
                _date(i["current_end"], "current_end"),
                i["group_by"],
                _filters(i["filters"]),
            ),
        ),
        Tool(
            "get_daily_series",
            "Daily cost for a date range, optionally filtered. Use it to see the shape of a "
            "change: step change, one-day spike, gradual growth, weekday pattern.",
            _schema(start=DATE, end=DATE, filters=FILTERS),
            lambda i: a.daily_series(
                records, _date(i["start"], "start"), _date(i["end"], "end"), _filters(i["filters"])
            ),
        ),
        Tool(
            "detect_anomalies",
            "Find cost spikes in a date range using a rolling statistical baseline per series. "
            "Returns episodes with expected vs actual daily cost, total excess and whether the "
            "episode is still ongoing. Group by usage_type to find the root cause of a spike.",
            _schema(start=DATE, end=DATE, group_by=GROUP_BY),
            lambda i: a.detect_anomalies(
                records, _date(i["start"], "start"), _date(i["end"], "end"), i["group_by"]
            ),
        ),
        Tool(
            "forecast_month_end",
            "Forecast the total for the month containing as_of (month-to-date plus trailing "
            "7-day run rate) and compare it with the previous month.",
            _schema(as_of=DATE),
            lambda i: a.forecast_month_end(records, _date(i["as_of"], "as_of")),
        ),
        Tool(
            "find_untagged_spend",
            "Cost with no value for a cost-allocation tag (team or environment), by service.",
            _schema(start=DATE, end=DATE, tag={"type": "string", "enum": ["team", "environment"]}),
            lambda i: a.untagged_spend(records, _date(i["start"], "start"), _date(i["end"], "end"), i["tag"]),
        ),
        Tool(
            "find_idle_resources",
            "Non-production resources with flat cost every day including weekends (always-on "
            "dev/staging), with estimated monthly savings if scheduled to business hours. Use a "
            "window of at least 14 days.",
            _schema(start=DATE, end=DATE),
            lambda i: a.idle_candidates(records, _date(i["start"], "start"), _date(i["end"], "end")),
        ),
    ]
    return {t.name: t for t in tools}
