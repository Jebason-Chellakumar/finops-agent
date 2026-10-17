from datetime import date

import pytest

from finops import sources
from finops.cli import main


class FakeCostExplorer:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_cost_and_usage(self, **params):
        self.calls.append(params)
        return self.pages[len(self.calls) - 1]


def group(service, tag, amount):
    return {"Keys": [service, tag], "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}}}


def test_cost_explorer_paginates_and_parses_tags():
    ce = FakeCostExplorer(
        [
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                        "Groups": [group("Amazon EC2", "team$checkout", 12.5), group("Amazon S3", "team$", 3)],
                    }
                ],
                "NextPageToken": "next",
            },
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-10-02", "End": "2026-10-03"},
                        "Groups": [group("Amazon EC2", "team$checkout", 0)],
                    }
                ]
            },
        ]
    )
    records = sources.load_cost_explorer(days=2, end=date(2026, 10, 3), client=ce)
    assert [(r.day, r.service, r.team, r.cost) for r in records] == [
        (date(2026, 10, 1), "Amazon EC2", "checkout", 12.5),
        (date(2026, 10, 1), "Amazon S3", "", 3.0),  # untagged
    ]
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-10-01", "End": "2026-10-03"}
    assert ce.calls[0]["GroupBy"][1] == {"Type": "TAG", "Key": "team"}
    assert ce.calls[1]["NextPageToken"] == "next"


def test_unknown_source():
    with pytest.raises(ValueError):
        sources.load("bigquery")


def test_cli_report_and_csv_source(tmp_path, capsys):
    csv_path = tmp_path / "costs.csv"
    assert main(["sample", "--out", str(csv_path)]) == 0
    assert main(["report", "--source", f"csv:{csv_path}"]) == 0
    out = capsys.readouterr().out
    assert "# FinOps report: 2026-10-15" in out
    assert "EC2 - Other / search" in out and "**ongoing**" in out
    assert "g5.2xlarge" in out and "Untagged spend" in out
