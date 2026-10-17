"""Where cost records come from: built-in sample, a CSV export, or AWS Cost Explorer."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import sample
from .records import CostRecord, read_csv

UNTAGGED = ""


def load(source: str, days: int = 90, tag_key: str = "team") -> list[CostRecord]:
    """``sample`` | ``csv:<path>`` | ``aws``."""
    if source == "sample":
        return sample.generate()
    if source.startswith("csv:"):
        return read_csv(source[4:])
    if source == "aws":
        return load_cost_explorer(days=days, tag_key=tag_key)
    raise ValueError(f"unknown source {source!r}; use sample, csv:<path> or aws")


def load_cost_explorer(
    days: int = 90,
    tag_key: str = "team",
    end: date | None = None,
    client: Any = None,
) -> list[CostRecord]:
    """Daily unblended cost grouped by service and a cost-allocation tag.

    Cost Explorer allows two GroupBy keys per query, so this loads service x tag. Each
    request costs $0.01, so the data is fetched once per run and analysed locally.
    Requires ``ce:GetCostAndUsage`` and an activated cost-allocation tag.
    """
    if client is None:
        import boto3  # optional dependency: only needed for live AWS data

        client = boto3.client("ce")
    end = end or date.today()  # Cost Explorer's End is exclusive
    start = end - timedelta(days=days)
    params = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "TAG", "Key": tag_key},
        ],
    }
    records: list[CostRecord] = []
    while True:
        page = client.get_cost_and_usage(**params)
        for result in page["ResultsByTime"]:
            day = date.fromisoformat(result["TimePeriod"]["Start"])
            for group in result.get("Groups", []):
                service, tag = group["Keys"]
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if cost == 0:
                    continue
                # Tag keys come back as "team$checkout"; "team$" means untagged.
                team = tag.split("$", 1)[1] if "$" in tag else UNTAGGED
                records.append(CostRecord(day=day, service=service, cost=cost, team=team))
        token = page.get("NextPageToken")
        if not token:
            return records
        params["NextPageToken"] = token
