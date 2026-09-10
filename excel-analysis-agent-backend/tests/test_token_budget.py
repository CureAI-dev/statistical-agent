"""Integration-level test for agent.py's MAX_RUN_TOKENS ceiling: a run that
burns past its token budget must stop, say so, and return nothing that
could be mistaken for a finding.

Same technique as test_step_limit.py - fake create_deep_agent's compiled
graph, so nothing here spends a real LLM call. The difference is what
drives the stop: the step limit trips on turn *count*, this trips on the
tokens those turns cost, which is the failure the step limit misses. A
measured run reached 1.83M tokens in 83 turns, well inside
MAX_PHASE2_STEPS=200 - turn count alone would never have caught it.
"""

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

import agent
import report
import store


class _FakeGraph:
    def __init__(self, steps):
        self._steps = steps

    def stream(self, _input, stream_mode="values", config=None):
        yield from self._steps


@pytest.fixture(autouse=True)
def _clean_store():
    stores = (
        store.HANDLES, store.SANDBOX_PATHS, store.SCHEMA_SIGS,
        store.CLASSIFICATIONS, store.SCALES, store.GROUPS,
    )
    for d in stores:
        d.clear()
    store.TOOL_CALLS.clear()
    yield
    for d in stores:
        d.clear()
    store.TOOL_CALLS.clear()


def _gate_ready(total_tokens=0):
    message = AIMessage(
        content="",
        tool_calls=[{
            "name": "submit_plan_tool",
            "args": {
                "status": "ready",
                "assumption": "test assumption",
                "tasks": [{"step": "report", "description": "d", "status": "pending"}],
            },
            "id": "call_1",
        }],
    )
    if total_tokens:
        message.usage_metadata = {
            "input_tokens": total_tokens, "output_tokens": 0, "total_tokens": total_tokens,
        }
    return {"messages": [message]}


def _expensive_turn(total_tokens):
    """A phase-2 turn that costs a lot and does not finish the analysis."""
    message = AIMessage(
        content="",
        tool_calls=[{"name": "run_code_tool", "args": {"code": "1 + 1"}, "id": "call_2"}],
    )
    message.usage_metadata = {
        "input_tokens": total_tokens, "output_tokens": 0, "total_tokens": total_tokens,
    }
    return {"messages": [message]}


def _final_answer(total_tokens):
    message = AIMessage(content="The analysis is complete: alpha = 0.83.")
    message.usage_metadata = {
        "input_tokens": total_tokens, "output_tokens": 0, "total_tokens": total_tokens,
    }
    return {"messages": [message]}


def _seed_handle(handle_id, sig):
    store.HANDLES[handle_id] = pd.DataFrame({"a": [1, 2, 3]})
    store.SANDBOX_PATHS[handle_id] = "/home/user/fake.csv"
    store.SCHEMA_SIGS[handle_id] = sig


def _patch(monkeypatch, tmp_path, gate_step, phase2_steps):
    def fake_create_deep_agent(*, model, tools, system_prompt, backend, middleware=None):
        if len(tools) <= 3:
            return _FakeGraph([gate_step])
        return _FakeGraph(phase2_steps)

    monkeypatch.setattr(agent, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(report, "OUTPUTS_DIR", tmp_path)


def test_phase2_over_budget_stops_and_reports_it(monkeypatch, tmp_path):
    handle_id, sig = "budget_handle", "budgetsig_phase2"
    _seed_handle(handle_id, sig)
    monkeypatch.setattr(agent, "MAX_RUN_TOKENS", 1_000)
    _patch(monkeypatch, tmp_path, _gate_ready(), [_expensive_turn(5_000)])

    result = agent.run(f"data/{handle_id}.csv", "burn the budget?", assume_and_state=True)

    assert result["status"] == "token_budget_exceeded"
    assert "token_budget_exceeded" in result["answer"]
    assert "5,000" in result["answer"] and "1,000" in result["answer"]


def test_over_budget_run_is_not_persisted_as_a_conclusion(monkeypatch, tmp_path):
    """A stopped run never reached a validated finding - filing it would
    hand it to a later run via recall_memory_tool as if it had."""
    handle_id, sig = "budget_handle2", "budgetsig_nopersist"
    _seed_handle(handle_id, sig)
    monkeypatch.setattr(agent, "MAX_RUN_TOKENS", 1_000)
    _patch(monkeypatch, tmp_path, _gate_ready(), [_expensive_turn(5_000)])

    agent.run(f"data/{handle_id}.csv", "burn the budget?", assume_and_state=True)

    from long_term_memory import recall
    assert recall(sig).get("conclusions") is None


def test_gate_phase_over_budget_never_starts_phase2(monkeypatch, tmp_path):
    handle_id, sig = "budget_handle3", "budgetsig_gate"
    _seed_handle(handle_id, sig)
    monkeypatch.setattr(agent, "MAX_RUN_TOKENS", 1_000)

    started_phase2 = []

    def fake_create_deep_agent(*, model, tools, system_prompt, backend, middleware=None):
        if len(tools) <= 3:
            return _FakeGraph([_gate_ready(total_tokens=9_000)])
        started_phase2.append(True)
        return _FakeGraph([_final_answer(10)])

    monkeypatch.setattr(agent, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(report, "OUTPUTS_DIR", tmp_path)

    result = agent.run(f"data/{handle_id}.csv", "blow it in the gate?", assume_and_state=True)

    assert result["status"] == "token_budget_exceeded"
    assert not started_phase2, "phase 2 must not run once the ceiling is already crossed"


def test_run_under_budget_completes_normally(monkeypatch, tmp_path):
    """The guardrail must not change the outcome of a healthy run - without
    this, a cap that always fired would still pass every test above."""
    handle_id, sig = "budget_handle4", "budgetsig_ok"
    _seed_handle(handle_id, sig)
    monkeypatch.setattr(agent, "MAX_RUN_TOKENS", 1_000_000)
    _patch(monkeypatch, tmp_path, _gate_ready(), [_final_answer(5_000)])

    result = agent.run(f"data/{handle_id}.csv", "a normal run", assume_and_state=True)

    assert result["status"] == "done"
    assert "alpha = 0.83" in result["answer"]
