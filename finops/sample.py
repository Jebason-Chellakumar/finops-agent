"""Deterministic sample AWS cost data with realistic, discoverable problems.

Service names match AWS Cost Explorer. The data hides five stories for the agents
(and the deterministic report) to find:

1. NAT Gateway data processing for the search team quadruples 10 days before the end.
2. A GPU instance (g5.2xlarge) left running 24/7 in data-platform dev for 25 days.
3. A one-day CloudWatch Logs spike in checkout prod (3 days before the end).
4. A staging RDS instance with flat cost every day, weekends included (idle).
5. ~$40/day of untagged EC2 spend nobody owns.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .records import CostRecord

SAMPLE_END = date(2026, 10, 15)
SAMPLE_DAYS = 120

EC2 = "Amazon Elastic Compute Cloud - Compute"
EC2_OTHER = "EC2 - Other"
RDS = "Amazon Relational Database Service"
S3 = "Amazon Simple Storage Service"
LAMBDA = "AWS Lambda"
CLOUDWATCH = "AmazonCloudWatch"
DYNAMODB = "Amazon DynamoDB"
GLUE = "AWS Glue"
ATHENA = "Amazon Athena"

PROD, NONPROD = "111111111111", "222222222222"

# service, team, environment, usage_type, base $/day, busier on weekdays?
BASELINE = [
    (EC2, "checkout", "prod", "BoxUsage:m6i.xlarge", 140.0, True),
    (RDS, "checkout", "prod", "InstanceUsage:db.r6g.large", 55.0, False),
    (CLOUDWATCH, "checkout", "prod", "DataProcessing-Bytes", 12.0, True),
    (LAMBDA, "checkout", "prod", "Request", 18.0, True),
    (DYNAMODB, "checkout", "prod", "ReadRequestUnits", 22.0, True),
    (EC2, "checkout", "staging", "BoxUsage:m6i.large", 24.0, True),
    (EC2, "search", "prod", "BoxUsage:c6i.2xlarge", 95.0, True),
    (EC2_OTHER, "search", "prod", "NatGateway-Bytes", 15.0, True),
    (S3, "search", "prod", "TimedStorage-ByteHrs", 9.0, False),
    (EC2, "search", "dev", "BoxUsage:t3.large", 11.0, True),
    (EC2, "data-platform", "prod", "BoxUsage:r6i.xlarge", 60.0, True),
    (GLUE, "data-platform", "prod", "ETL-DPU-Hour", 48.0, True),
    (ATHENA, "data-platform", "prod", "DataScannedInTB", 14.0, True),
    (EC2, "", "", "BoxUsage:m5.xlarge", 40.0, False),  # untagged
]


def generate(end: date = SAMPLE_END, days: int = SAMPLE_DAYS, seed: int = 7) -> list[CostRecord]:
    rng = random.Random(seed)
    start = end - timedelta(days=days - 1)
    records: list[CostRecord] = []

    def add(day, service, team, env, usage, cost):
        account = PROD if env == "prod" else NONPROD
        region = "us-east-1" if service == CLOUDWATCH else "ap-south-1"
        records.append(CostRecord(day, service, round(cost, 4), account, region, team, env, usage))

    for i in range(days):
        day = start + timedelta(days=i)
        days_to_end = (end - day).days
        weekday = day.weekday() < 5

        for service, team, env, usage, base, seasonal in BASELINE:
            cost = base * (1.08 if seasonal and weekday else 0.85 if seasonal else 1.0)
            cost *= 1 + rng.gauss(0, 0.04)
            if (service, team, usage) == (EC2_OTHER, "search", "NatGateway-Bytes") and days_to_end < 10:
                cost *= 4.0  # story 1
            if service == CLOUDWATCH and days_to_end == 3:
                cost *= 5.0  # story 3
            add(day, service, team, env, usage, cost)

        # Steady storage growth in the data lake (not an anomaly).
        add(day, S3, "data-platform", "prod", "TimedStorage-ByteHrs", 30.0 * 1.005**i)

        # Story 4: idle staging database, identical cost every day.
        add(day, RDS, "checkout", "staging", "InstanceUsage:db.m6g.large", 18.24)

        # Story 2: forgotten GPU box in dev.
        if days_to_end < 25:
            add(day, EC2, "data-platform", "dev", "BoxUsage:g5.2xlarge", 29.06)

    return records
