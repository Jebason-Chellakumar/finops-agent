from datetime import date, timedelta

import pytest

from finops import analysis as a
from finops import sample
from finops.records import CostRecord, read_csv, write_csv

END = sample.SAMPLE_END
RECORDS = sample.generate()


def d(days_before_end: int) -> date:
    return END - timedelta(days=days_before_end)


def test_sample_is_deterministic():
    assert sample.generate() == RECORDS


def test_csv_round_trip(tmp_path):
    path = tmp_path / "costs.csv"
    write_csv(RECORDS[:50], path)
    back = read_csv(path)
    assert [(r.day, r.service, r.team) for r in back] == [(r.day, r.service, r.team) for r in RECORDS[:50]]
    assert all(abs(x.cost - y.cost) < 1e-4 for x, y in zip(back, RECORDS[:50]))


def test_dataset_info():
    info = a.dataset_info(RECORDS)
    assert info["last_day"] == END.isoformat()
    assert info["days"] == sample.SAMPLE_DAYS
    assert "(none)" in info["dimensions"]["team"]  # untagged spend exists
    assert "data-platform" in info["dimensions"]["team"]


def test_summarize_shares_add_up():
    s = a.summarize(RECORDS, d(29), END, ["team"], top_n=20)
    assert abs(sum(g["cost"] for g in s["groups"]) - s["total"]) < 0.1
    assert abs(sum(g["share_pct"] for g in s["groups"]) - 100) < 0.5


def test_summarize_filters_untagged_with_none_label():
    s = a.summarize(RECORDS, d(29), END, ["service"], filters={"team": ""})
    assert [g["service"] for g in s["groups"]] == [sample.EC2]


def test_compare_periods_finds_the_real_drivers():
    c = a.compare_periods(RECORDS, d(59), d(30), d(29), END, ["service", "team"], top_n=3)
    top = [(x["service"], x["team"]) for x in c["drivers"]]
    assert top[:2] == [(sample.EC2, "data-platform"), (sample.EC2_OTHER, "search")]
    assert c["delta_daily_total"] > 0


def test_detects_planted_anomalies_without_false_positives():
    found = a.detect_anomalies(RECORDS, d(29), END)["anomalies"]
    by_key = {(x["service"], x["team"]): x for x in found}
    assert set(by_key) == {
        (sample.EC2_OTHER, "search"),
        (sample.EC2, "data-platform"),
        (sample.CLOUDWATCH, "checkout"),
    }
    nat = by_key[(sample.EC2_OTHER, "search")]
    assert nat["start"] == d(9).isoformat() and nat["ongoing"] and nat["days"] == 10
    gpu = by_key[(sample.EC2, "data-platform")]
    assert gpu["start"] == d(24).isoformat() and gpu["ongoing"]  # weekends don't split it
    spike = by_key[(sample.CLOUDWATCH, "checkout")]
    assert spike["days"] == 1 and not spike["ongoing"]


def test_quiet_period_has_no_anomalies():
    assert a.detect_anomalies(RECORDS, d(89), d(30))["anomalies"] == []


def test_root_cause_by_usage_type():
    found = a.detect_anomalies(RECORDS, d(29), END, group_by=["usage_type"])["anomalies"]
    assert "NatGateway-Bytes" in {x["usage_type"] for x in found}
    assert "BoxUsage:g5.2xlarge" in {x["usage_type"] for x in found}


def test_idle_candidates_are_flat_non_prod_only():
    c = a.idle_candidates(RECORDS, d(13), END)["candidates"]
    assert [(x["usage_type"], x["environment"]) for x in c] == [
        ("BoxUsage:g5.2xlarge", "dev"),
        ("InstanceUsage:db.m6g.large", "staging"),
    ]
    assert c[0]["est_monthly_savings_if_scheduled"] == pytest.approx(29.06 * 30 * (1 - 50 / 168), abs=0.01)


def test_forecast_month_end():
    f = a.forecast_month_end(RECORDS, END)
    assert f["month_to_date"] < f["forecast_month_total"]
    expected = f["month_to_date"] + f["run_rate_daily"] * (31 - END.day)
    assert f["forecast_month_total"] == pytest.approx(expected, abs=0.1)


def test_untagged_spend():
    u = a.untagged_spend(RECORDS, d(29), END)
    assert u["by_service"][0]["service"] == sample.EC2
    assert 0 < u["share_pct"] < 10


def test_daily_series_fills_missing_days_with_zero():
    rows = [CostRecord(date(2026, 1, 1), "S", 5.0), CostRecord(date(2026, 1, 3), "S", 7.0)]
    s = a.daily_series(rows, date(2026, 1, 1), date(2026, 1, 3))
    assert [p["cost"] for p in s["series"]] == [5.0, 0.0, 7.0]


@pytest.mark.parametrize(
    "call",
    [
        lambda: a.summarize(RECORDS, END, d(5), ["service"]),
        lambda: a.summarize(RECORDS, d(5), END, ["colour"]),
        lambda: a.select(RECORDS, d(5), END, {"colour": "red"}),
        lambda: a.date_range([]),
    ],
)
def test_bad_input_raises_analysis_error(call):
    with pytest.raises(a.AnalysisError):
        call()
