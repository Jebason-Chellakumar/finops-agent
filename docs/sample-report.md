# FinOps report: 2026-10-15

**Last 30 days:** $20,421 ($681/day, +7.6% vs previous 30 days)  
**Month-end forecast:** $22,103 (+13.8% vs last month's $19,424)

## Top services (last 30 days)
| service | cost | share |
|---|---:|---:|
| Amazon Elastic Compute Cloud - Compute | $11,970 | 58.6% |
| Amazon Relational Database Service | $2,191 | 10.7% |
| Amazon Simple Storage Service | $1,787 | 8.8% |
| AWS Glue | $1,482 | 7.3% |
| EC2 - Other | $924 | 4.5% |
| Amazon DynamoDB | $676 | 3.3% |

## Biggest changes (daily average, vs previous 30 days)
| service | team | before/day | now/day | change per 30 days |
|---|---|---:|---:|---:|
| Amazon Elastic Compute Cloud - Compute | data-platform | $61 | $85 | +$722 (+39.5%) |
| EC2 - Other | search | $15 | $31 | +$467 (+102.4%) |
| Amazon Simple Storage Service | data-platform | $44 | $51 | +$211 (+16.1%) |
| AmazonCloudWatch | checkout | $12 | $14 | +$54 (+14.8%) |
| AWS Glue | data-platform | $48 | $49 | +$45 (+3.1%) |

## Anomalies (last 30 days)
- **Amazon Elastic Compute Cloud - Compute / data-platform**: $61/day → $90/day from 2026-09-21 (25 days, **ongoing**), excess $726
- **EC2 - Other / search**: $15/day → $62/day from 2026-10-06 (10 days, **ongoing**), excess $465
- **AmazonCloudWatch / checkout**: $12/day → $65/day from 2026-10-12 (1 day, ended 2026-10-12), excess $52

## Savings opportunities
- **Always-on dev**: Amazon Elastic Compute Cloud - Compute `BoxUsage:g5.2xlarge` (data-platform, account 222222222222) costs $872/month at a flat rate every day. Scheduling to business hours saves ~$612/month.
- **Always-on staging**: Amazon Relational Database Service `InstanceUsage:db.m6g.large` (checkout, account 222222222222) costs $547/month at a flat rate every day. Scheduling to business hours saves ~$384/month.
- **Untagged spend**: $1,194 (5.8%) in the last 30 days has no `team` tag, mostly Amazon Elastic Compute Cloud - Compute. Enforce tags with an AWS Organizations tag policy.

_Anomalies: rolling 14-day baseline, z>3.0, excess>=$20.0/day. Scheduling savings assume ~50 of 168 hours/week._
