"""Multi-agent FinOps assistant on the Claude API.

An orchestrator agent plans the investigation and delegates to three specialists,
each with its own instructions and a narrow set of cost-data tools:

    orchestrator ─┬─ cost_analyst          where the money goes, what changed, forecast
                  ├─ anomaly_investigator  spikes, their shape and root cause
                  └─ optimization_advisor  idle resources, tagging gaps, savings

Specialists run in parallel when the orchestrator delegates to several at once. Every
figure comes from deterministic tools (``finops.analysis``); the models decide what
to look at and explain it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .analysis import AnalysisError
from .records import CostRecord
from .tools import Tool, build_tools

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000

# USD per million tokens: input, output, cache read, cache write (5-minute TTL).
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
}

EventHandler = Callable[[str, str], None]


class AgentError(RuntimeError):
    pass


class AgentRefusal(AgentError):
    pass


# ---------------------------------------------------------------- usage accounting


@dataclass
class UsageLedger:
    """Token usage and estimated cost per agent. A FinOps tool should know its own bill."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, agent: str, model: str, usage: Any) -> None:
        get = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
        with self._lock:
            row = self.rows.setdefault(
                agent,
                {"model": model, "requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            )
            row["requests"] += 1
            row["input"] += get("input_tokens")
            row["output"] += get("output_tokens")
            row["cache_read"] += get("cache_read_input_tokens")
            row["cache_write"] += get("cache_creation_input_tokens")

    def cost(self, agent: str) -> float | None:
        row = self.rows[agent]
        prices = PRICING.get(row["model"])
        if prices is None:
            return None
        tokens = (row["input"], row["output"], row["cache_read"], row["cache_write"])
        return sum(t * p for t, p in zip(tokens, prices)) / 1_000_000

    def total_cost(self) -> float | None:
        costs = [self.cost(a) for a in self.rows]
        return None if any(c is None for c in costs) else sum(costs)

    def table(self) -> str:
        lines = [
            "| agent | model | requests | input | output | cache read | cache write | est. cost |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name, r in self.rows.items():
            cost = self.cost(name)
            lines.append(
                f"| {name} | {r['model']} | {r['requests']} | {r['input']:,} | {r['output']:,} | "
                f"{r['cache_read']:,} | {r['cache_write']:,} | {'n/a' if cost is None else f'${cost:.4f}'} |"
            )
        total = self.total_cost()
        lines.append(f"\nEstimated total: {'n/a' if total is None else f'${total:.4f}'}")
        return "\n".join(lines)


# ---------------------------------------------------------------- agent loop


@dataclass
class AgentSpec:
    name: str
    system: str
    tools: list[Tool]
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_turns: int = 12


class Agent:
    """A tool-using agent: call the model, run requested tools, repeat until done.

    History is append-only (assistant content is passed back exactly as returned,
    thinking blocks included), and the request prefix (tools + system) never changes,
    so prompt caching hits on every turn after the first.
    """

    def __init__(
        self,
        spec: AgentSpec,
        client: Any,
        ledger: UsageLedger,
        use_fallbacks: bool = True,
        on_event: EventHandler | None = None,
    ):
        self.spec = spec
        self.client = client
        self.ledger = ledger
        self.use_fallbacks = use_fallbacks
        self.on_event = on_event or (lambda agent, msg: None)
        self._tools = {t.name: t for t in spec.tools}
        self._tool_defs = [t.to_api() for t in spec.tools]

    def run(self, task: str) -> str:
        messages: list[dict] = [{"role": "user", "content": task}]
        for _ in range(self.spec.max_turns):
            response = self._create(messages)
            # Bill against the model that actually served the turn (a fallback may have).
            self.ledger.record(self.spec.name, getattr(response, "model", self.spec.model), response.usage)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None)
                raise AgentRefusal(f"{self.spec.name} declined the request (category: {category})")
            if response.stop_reason == "max_tokens":
                raise AgentError(f"{self.spec.name} hit max_tokens before finishing")

            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                return _text(response.content)
            messages.append({"role": "user", "content": self._run_tools(calls)})
        raise AgentError(f"{self.spec.name} did not finish within {self.spec.max_turns} turns")

    def _create(self, messages: list[dict]):
        kwargs = dict(
            model=self.spec.model,
            max_tokens=MAX_TOKENS,
            system=self.spec.system,
            tools=self._tool_defs,
            messages=messages,
            output_config={"effort": self.spec.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.use_fallbacks:
            # Server-side fallback: if a safety classifier declines, the API retries on
            # Anthropic's recommended model for that category within the same call.
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.beta.messages.create(**kwargs)

    def _run_tools(self, calls: Sequence[Any]) -> list[dict]:
        # All results go back in ONE user message, in call order.
        if len(calls) == 1:
            return [self._run_tool(calls[0])]
        with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as pool:
            return list(pool.map(self._run_tool, calls))

    def _run_tool(self, call: Any) -> dict:
        tool = self._tools.get(call.name)
        self.on_event(self.spec.name, f"{call.name}({_brief(call.input)})")
        try:
            if tool is None:
                raise AnalysisError(f"unknown tool {call.name!r}")
            result = tool.handler(dict(call.input))
            content = result if isinstance(result, str) else json.dumps(result)
            return {"type": "tool_result", "tool_use_id": call.id, "content": content}
        except (AnalysisError, AgentError) as err:
            # Expected failures go back to the model so it can correct course.
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(err), "is_error": True}


def _text(content: Sequence[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _brief(payload: Any, limit: int = 90) -> str:
    text = json.dumps(payload, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- the team

SHARED_RULES = """\
Rules:
- Every number you state must come from a tool result. Never estimate, extrapolate or do \
mental arithmetic beyond simple sums of tool outputs; if a figure isn't available, say so.
- Dates are inclusive ISO dates. Call get_dataset_info first if you don't know the date range \
or valid filter values.
- Be specific: name the service, team, environment, usage type and dates.
- Keep your final answer under 250 words, as Markdown bullets, with dollar amounts rounded to \
whole dollars."""

COST_ANALYST = f"""\
You are the cost analyst on a FinOps team analysing AWS spend.
Your job: explain where the money goes, what changed between periods and why, and where the \
month is heading. Break totals down until the drivers are clear (service -> team -> usage type).

{SHARED_RULES}"""

ANOMALY_INVESTIGATOR = f"""\
You are the anomaly investigator on a FinOps team analysing AWS spend.
Your job: find unexpected spend, characterise each anomaly (one-day spike, sustained step \
change, gradual drift), find its root cause by drilling into usage_type, quantify the excess, \
and say whether it is still ongoing. Give a concrete likely cause and a next step for each \
(e.g. NatGateway-Bytes growth -> check for new cross-AZ or internet egress traffic; consider \
VPC endpoints for S3/DynamoDB).

{SHARED_RULES}"""

OPTIMIZATION_ADVISOR = f"""\
You are the optimization advisor on a FinOps team analysing AWS spend.
Your job: find savings. Look for always-on non-production resources, untagged spend that \
blocks accountability, and fast-growing costs. For each recommendation give the resource \
(service, team, environment, usage type, account), the estimated monthly saving from the \
tools, the concrete AWS action (e.g. Instance Scheduler, stop RDS out of hours, S3 lifecycle \
to Intelligent-Tiering, tag policies in AWS Organizations) and the risk. Rank by saving.

{SHARED_RULES}"""

ORCHESTRATOR = """\
You are the lead of a FinOps team answering questions about an organization's AWS bill.
You have three specialists, each with their own cost-data tools:
- cost_analyst: breakdowns, period-over-period changes and their drivers, month-end forecast
- anomaly_investigator: spikes and step changes, root cause, whether still ongoing
- optimization_advisor: idle non-prod resources, untagged spend, savings estimates

How to work:
- Delegate. Give each specialist a precise, self-contained task with explicit dates (they \
cannot see this conversation). When several specialists are needed, call them in the same turn \
so they run in parallel.
- Use get_dataset_info yourself if you need the date range before delegating.
- Then write the final answer for an engineering manager: start with a 2-3 sentence summary, \
then sections only as needed (What changed, Anomalies, Recommendations with estimated monthly \
savings, Next steps). Use only figures the specialists reported. Under 400 words, Markdown."""

SPECIALISTS = {
    "cost_analyst": (
        COST_ANALYST,
        ["get_dataset_info", "summarize_costs", "compare_periods", "get_daily_series", "forecast_month_end"],
        "Breakdowns, period-over-period changes with drivers, and month-end forecast.",
    ),
    "anomaly_investigator": (
        ANOMALY_INVESTIGATOR,
        ["get_dataset_info", "detect_anomalies", "get_daily_series", "summarize_costs"],
        "Finds cost spikes and step changes, drills into usage type for root cause.",
    ),
    "optimization_advisor": (
        OPTIMIZATION_ADVISOR,
        ["get_dataset_info", "find_idle_resources", "find_untagged_spend", "summarize_costs", "compare_periods"],
        "Finds savings: always-on non-prod resources, untagged spend, fast-growing costs.",
    ),
}


class FinOpsTeam:
    def __init__(
        self,
        records: Sequence[CostRecord],
        client: Any,
        model: str = DEFAULT_MODEL,
        worker_model: str | None = None,
        use_fallbacks: bool = True,
        on_event: EventHandler | None = None,
    ):
        self.ledger = UsageLedger()
        self._client = client
        self._fallbacks = use_fallbacks
        self._on_event = on_event
        data_tools = build_tools(records)

        self.specialists: dict[str, Agent] = {}
        for name, (system, tool_names, _) in SPECIALISTS.items():
            spec = AgentSpec(name, system, [data_tools[t] for t in tool_names], worker_model or model, "medium")
            self.specialists[name] = self._agent(spec)

        delegate_tools = [self._delegate_tool(name, desc) for name, (_, _, desc) in SPECIALISTS.items()]
        self.orchestrator = self._agent(
            AgentSpec(
                "orchestrator",
                ORCHESTRATOR,
                [data_tools["get_dataset_info"], *delegate_tools],
                model,
                "high",
                max_turns=8,
            )
        )

    def ask(self, question: str, as_of: str | None = None) -> str:
        # Volatile context goes in the user turn, never the system prompt (keeps caching intact).
        prefix = f"Today is {as_of}. " if as_of else ""
        return self.orchestrator.run(prefix + question)

    def _agent(self, spec: AgentSpec) -> Agent:
        return Agent(spec, self._client, self.ledger, self._fallbacks, self._on_event)

    def _delegate_tool(self, name: str, description: str) -> Tool:
        def handler(inp: dict) -> str:
            try:
                return self.specialists[name].run(inp["task"])
            except AgentRefusal as err:
                raise AgentError(str(err)) from None

        return Tool(
            f"ask_{name}",
            f"Delegate a task to the {name}. {description} Returns its findings as Markdown.",
            {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Self-contained task with explicit ISO dates and what to report back.",
                    }
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler,
        )
