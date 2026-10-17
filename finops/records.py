"""The cost record model shared by every data source and analysis."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

DIMENSIONS = ("service", "account", "region", "team", "environment", "usage_type")


@dataclass(frozen=True)
class CostRecord:
    """One day of unblended cost for one combination of dimensions (USD)."""

    day: date
    service: str
    cost: float
    account: str = ""
    region: str = ""
    team: str = ""  # cost-allocation tag; "" means untagged
    environment: str = ""
    usage_type: str = ""


def write_csv(records: Iterable[CostRecord], path: str | Path) -> None:
    names = [f.name for f in fields(CostRecord)]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        for r in records:
            row = asdict(r)
            row["day"] = r.day.isoformat()
            row["cost"] = f"{r.cost:.4f}"
            writer.writerow(row)


def read_csv(path: str | Path) -> list[CostRecord]:
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(
                CostRecord(
                    day=date.fromisoformat(row["day"]),
                    service=row["service"],
                    cost=float(row["cost"]),
                    **{d: row.get(d, "") or "" for d in DIMENSIONS if d != "service"},
                )
            )
    return out
