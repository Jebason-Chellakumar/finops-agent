"""Agent loop and orchestration, driven by a scripted fake Claude client (no network)."""

import json
import threading
from types import SimpleNamespace as NS

import pytest

from finops import sample
from finops.agents import (
    FALLBACK_BETA,
    SPECIALISTS,
    Agent,
    AgentError,
    AgentRefusal,
    AgentSpec,
    FinOpsTeam,
    UsageLedger,
)
from finops.tools import build_tools

RECORDS = sample.generate()


def text(t):
    return NS(type="text", text=t)


def tool_use(name, inp, id_):
    return NS(type="tool_use", name=name, input=inp, id=id_)


def response(*content, stop="end_turn", model="claude-opus-5-5"):
    usage = NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=900, cache_creation_input_tokens=0)
    return NS(content=list(content), stop_reason=stop, usage=usage, model=model, stop_details=None)


class FakeClient:
    """Routes each request to a per-agent script, keyed by the agent's system prompt."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.requests = []
        self._lock = threading.Lock()
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same messages list after the call.
        kwargs = {**kwargs, "messages": list(kwargs["messages"])}
        with self._lock:
            self.requests.append(kwargs)
            for marker, script in self.scripts.items():
                if marker in kwargs["system"]:
                    return script.pop(0)
        raise AssertionError("no script for system prompt")


def last_tool_results(request):
    return request["messages"][-1]["content"]


# ---------------------------------------------------------------- single agent


def make_agent(script, **kw):
    tools = build_tools(RECORDS)
    spec = AgentSpec("analyst", "You are a test analyst.", [tools["summarize_costs"], tools["get_dataset_info"]])
    client = FakeClient({"test analyst": script})
    return Agent(spec, client, UsageLedger(), **kw), client


FILTERS_NONE = {d: None for d in ("service", "account", "region", "team", "environment", "usage_type")}


def test_agent_runs_tools_and_returns_final_text():
    agent, client = make_agent(
        [
            response(
                tool_use(
                    "summarize_costs",
                    {"start": "2026-10-01", "end": "2026-10-15", "group_by": ["team"], "filters": FILTERS_NONE},
                    "t1",
                ),
                stop="tool_use",
            ),
            response(text("Checkout is the biggest team.")),
        ]
    )
    assert agent.run("Who spends most?") == "Checkout is the biggest team."

    second = client.requests[1]
    result = last_tool_results(second)[0]
    assert result["tool_use_id"] == "t1" and "is_error" not in result
    assert json.loads(result["content"])["groups"][0]["team"]
    # history is append-only: user, assistant(tool_use), user(tool_result)
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]


def test_request_shape_caching_effort_strict_tools_and_fallbacks():
    agent, client = make_agent([response(text("ok"))])
    agent.run("hi")
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"] == {"effort": "medium"}
    assert req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"
    assert all(t["strict"] for t in req["tools"])
    assert "tool_choice" not in req  # forced tool choice is rejected on this model


def test_fallbacks_can_be_disabled():
    agent, client = make_agent([response(text("ok"))], use_fallbacks=False)
    agent.run("hi")
    assert "fallbacks" not in client.requests[0] and "betas" not in client.requests[0]


def test_tool_errors_are_returned_to_the_model():
    agent, client = make_agent(
        [
            response(
                tool_use(
                    "summarize_costs",
                    {"start": "15/10/2026", "end": "2026-10-15", "group_by": ["team"], "filters": FILTERS_NONE},
                    "bad",
                ),
                tool_use("no_such_tool", {}, "missing"),
                stop="tool_use",
            ),
            response(text("Sorry, retrying with a valid date.")),
        ]
    )
    agent.run("q")
    results = last_tool_results(client.requests[1])
    assert [r["tool_use_id"] for r in results] == ["bad", "missing"]  # order preserved, one message
    assert all(r["is_error"] for r in results)
    assert "YYYY-MM-DD" in results[0]["content"]


def test_refusal_raises_before_reading_content():
    agent, _ = make_agent([response(stop="refusal")])
    with pytest.raises(AgentRefusal):
        agent.run("q")


def test_max_tokens_and_turn_limit():
    agent, _ = make_agent([response(text("partial"), stop="max_tokens")])
    with pytest.raises(AgentError, match="max_tokens"):
        agent.run("q")

    loop = [response(tool_use("get_dataset_info", {}, f"t{i}"), stop="tool_use") for i in range(20)]
    agent, _ = make_agent(loop)
    agent.spec.max_turns = 3
    with pytest.raises(AgentError, match="3 turns"):
        agent.run("q")


def test_usage_ledger_costs_by_serving_model():
    ledger = UsageLedger()
    ledger.record("a", "claude-opus-5-5", NS(input_tokens=1_000_000, output_tokens=0))
    ledger.record("b", "claude-sonnet-5-5", NS(output_tokens=1_000_000, cache_read_input_tokens=1_000_000))
    assert ledger.cost("a") == pytest.approx(4.00)
    assert ledger.cost("b") == pytest.approx(10.20)
    assert ledger.total_cost() == pytest.approx(14.20)
    ledger.record("c", "some-unknown-model", NS(input_tokens=1))
    assert ledger.total_cost() is None
    assert "| a | claude-opus-5-5 | 1 |" in ledger.table()


# ---------------------------------------------------------------- team


def test_orchestrator_delegates_in_parallel_and_synthesizes():
    client = FakeClient(
        {
            "lead of a FinOps team": [
                response(
                    tool_use("ask_anomaly_investigator", {"task": "Find anomalies 2026-09-16..2026-10-15"}, "o1"),
                    tool_use("ask_optimization_advisor", {"task": "Find savings 2026-10-02..2026-10-15"}, "o2"),
                    stop="tool_use",
                ),
                response(text("**Summary:** NAT egress spike and an idle GPU box.")),
            ],
            "anomaly investigator": [
                response(
                    tool_use(
                        "detect_anomalies",
                        {"start": "2026-09-16", "end": "2026-10-15", "group_by": ["usage_type"]},
                        "a1",
                    ),
                    stop="tool_use",
                ),
                response(text("- NatGateway-Bytes up 4x since 2026-10-06")),
            ],
            "optimization advisor": [
                response(
                    tool_use("find_idle_resources", {"start": "2026-10-02", "end": "2026-10-15"}, "s1"),
                    stop="tool_use",
                ),
                response(text("- Schedule the g5.2xlarge dev box: ~$612/month")),
            ],
        }
    )
    events = []
    team = FinOpsTeam(RECORDS, client, worker_model="claude-sonnet-5-5", on_event=lambda a, m: events.append(a))
    answer = team.ask("Why is our bill up?", as_of="2026-10-15")

    assert answer.startswith("**Summary:**")
    # specialist reports came back to the orchestrator as tool results, in order
    results = last_tool_results(client.requests[-1])
    assert [r["tool_use_id"] for r in results] == ["o1", "o2"]
    assert "NatGateway-Bytes" in results[0]["content"] and "$612/month" in results[1]["content"]
    # specialists really ran the analysis
    anomaly_result = next(
        r for r in client.requests if "anomaly investigator" in r["system"] and len(r["messages"]) == 3
    )
    assert "NatGateway-Bytes" in last_tool_results(anomaly_result)[0]["content"]
    # models, effort and usage are tracked per agent
    assert {r["model"] for r in client.requests if "lead of" not in r["system"]} == {"claude-sonnet-5-5"}
    assert set(team.ledger.rows) == {"orchestrator", "anomaly_investigator", "optimization_advisor"}
    assert {"orchestrator", "anomaly_investigator", "optimization_advisor"} <= set(events)
    # the date goes in the user turn, keeping the system prompt cacheable
    assert client.requests[0]["messages"][0]["content"].startswith("Today is 2026-10-15.")


def test_specialist_refusal_becomes_tool_error_for_orchestrator():
    client = FakeClient(
        {
            "lead of a FinOps team": [
                response(tool_use("ask_cost_analyst", {"task": "x"}, "o1"), stop="tool_use"),
                response(text("The analyst could not help.")),
            ],
            "cost analyst": [response(stop="refusal")],
        }
    )
    FinOpsTeam(RECORDS, client).ask("q")
    result = last_tool_results(client.requests[-1])[0]
    assert result["is_error"] and "declined" in result["content"]


def test_specialist_tool_sets_are_scoped():
    team = FinOpsTeam(RECORDS, FakeClient({}))
    for name, (_, tool_names, _) in SPECIALISTS.items():
        assert sorted(team.specialists[name]._tools) == sorted(tool_names)
    assert sorted(team.orchestrator._tools) == [
        "ask_anomaly_investigator",
        "ask_cost_analyst",
        "ask_optimization_advisor",
        "get_dataset_info",
    ]


def test_tool_schemas_are_strict_compatible():
    def check(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert sorted(schema["required"]) == sorted(schema["properties"])
            for prop in schema["properties"].values():
                check(prop)

    for tool in build_tools(RECORDS).values():
        check(tool.input_schema)
