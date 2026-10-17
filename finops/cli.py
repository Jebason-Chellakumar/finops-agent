"""Command line: ``finops report | ask | sample``."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import sources
from .agents import DEFAULT_MODEL, AgentError, FinOpsTeam
from .analysis import date_range
from .records import write_csv
from .report import build_report


def _progress(agent: str, message: str) -> None:
    print(f"  · {agent:<21} {message}", file=sys.stderr, flush=True)


def cmd_report(args) -> int:
    records = sources.load(args.source, days=args.days)
    text = build_report(records, date.fromisoformat(args.as_of) if args.as_of else None)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_ask(args) -> int:
    import anthropic  # imported here so `report` works without the SDK installed

    records = sources.load(args.source, days=args.days)
    as_of = args.as_of or date_range(records)[1].isoformat()
    team = FinOpsTeam(
        records,
        anthropic.Anthropic(),
        model=args.model,
        worker_model=args.worker_model,
        use_fallbacks=not args.no_fallbacks,
        on_event=None if args.quiet else _progress,
    )
    try:
        answer = team.ask(args.question, as_of=as_of)
    except AgentError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("error: could not reach the Claude API", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as err:
        print(f"error: Claude API returned {err.status_code}: {err.message}", file=sys.stderr)
        return 1
    print("\n" + answer + "\n")
    if args.show_usage:
        print(team.ledger.table())
    return 0


def cmd_sample(args) -> int:
    from .sample import generate

    write_csv(generate(), args.out)
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finops", description="Multi-agent FinOps assistant for AWS costs")
    sub = parser.add_subparsers(dest="command", required=True)

    def data_args(p):
        p.add_argument("--source", default="sample", help="sample | csv:<path> | aws (default: sample)")
        p.add_argument("--days", type=int, default=90, help="days of history to load from AWS")
        p.add_argument("--as-of", help="analysis date YYYY-MM-DD (default: last day in the data)")

    p = sub.add_parser("report", help="deterministic Markdown report (no API key needed)")
    data_args(p)
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ask", help="ask the agent team a question")
    p.add_argument("question")
    data_args(p)
    p.add_argument("--model", default=DEFAULT_MODEL, help="orchestrator model")
    p.add_argument("--worker-model", help="specialist model (default: same as --model)")
    p.add_argument("--no-fallbacks", action="store_true", help="disable server-side refusal fallbacks")
    p.add_argument("--show-usage", action="store_true", help="print token usage and estimated cost")
    p.add_argument("--quiet", action="store_true", help="hide agent progress")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("sample", help="write the sample dataset to CSV")
    p.add_argument("--out", default="sample_costs.csv")
    p.set_defaults(func=cmd_sample)

    args = parser.parse_args(argv)
    return args.func(args)
