"""Integration-level test for agent.py's FR-7.3 step-limit guardrail
(P0-2, see docs/improvement-plan.md): a run that hits its recursion limit
must return a graceful partial result, not crash.

Verified without spending a real LLM/E2B call, by faking
create_deep_agent's returned graph object - it's a plain object with a
.stream() method that yields {"messages": [...]} dicts (exactly what
stream_mode="values" gives agent.py's loops) and, when configured to,
raises langgraph.errors.GraphRecursionError partway through, the same
shape a real graph raises once config["recursion_limit"] is exceeded.
"""

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

import agent
import report
import store


class _FakeGraph:
    """Stands in for create_deep_agent's compiled graph."""

    def __init__(self, steps, raise_after=None):
        self._steps = steps
        self._raise_after = raise_after

    def stream(self, _input, stream_mode="values", config=None):
        for i, step in enumerate(self._steps):
            yield step
            if self._raise_after is not None and i + 1 >= self._raise_after:
                raise GraphRecursionError("recursion limit reached (fake)")


@pytest.fixture(autouse=True)
def _clean_store():
    for d in (store.HANDLES, store.SANDBOX_PATHS, store.SCHEMA_SIGS, store.CLASSIFICATIONS, store.SCALES, store.GROUPS):
        d.clear()
    store.TOOL_CALLS.clear()
    yield
    for d in (store.HANDLES, store.SANDBOX_PATHS, store.SCHEMA_SIGS, store.CLASSIFICATIONS, store.SCALES, store.GROUPS):
        d.clear()
    store.TOOL_CALLS.clear()


def _ready_gate_step():
    """The gate phase immediately commits status="ready" - the minimum
    needed to reach phase 2."""
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_plan_tool",
                "args": {
                    "status": "ready",
                    "assumption": "test assumption",
                    "tasks": [{"step": "report", "description": "d", "status": "pending"}],
                },
                "id": "call_1",
            }
        ],
    )
    return {"messages": [message]}


def _looping_tool_call_step():
    """A phase-2 step that keeps the loop going (never a final answer) -
    stands in for what a stuck retry loop looks like turn to turn."""
    message = AIMessage(
        content="",
        tool_calls=[{"name": "run_code_tool", "args": {"code": "1 + 1"}, "id": "call_2"}],
    )
    return {"messages": [message]}


def test_phase2_recursion_limit_returns_graceful_partial_result(monkeypatch, tmp_path):
    handle_id = "fake_handle"
    store.HANDLES[handle_id] = pd.DataFrame({"a": [1, 2, 3]})
    store.SANDBOX_PATHS[handle_id] = "/home/user/fake.csv"
    store.SCHEMA_SIGS[handle_id] = "fakesig"

    def fake_create_deep_agent(*, model, tools, system_prompt, backend, middleware=None):
        if len(tools) <= 3:  # gate phase: read_excel_tool, profile_tool, submit_plan_tool
            return _FakeGraph(steps=[_ready_gate_step()])
        return _FakeGraph(steps=[_looping_tool_call_step()], raise_after=1)

    monkeypatch.setattr(agent, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(report, "OUTPUTS_DIR", tmp_path)

    result = agent.run(f"data/{handle_id}.csv", "does this hit the cap?", assume_and_state=True)

    assert result["status"] == "step_limit_exceeded"
    assert "step_limit_exceeded" in result["answer"]
    assert str(agent.MAX_PHASE2_STEPS) in result["answer"]
    # A run that never produced a real finding must not be filed as one -
    # see agent.py's run(), the "Skip persisting a conclusion..." comment.
    assert store.SCHEMA_SIGS[handle_id] == "fakesig"  # sanity: fixture wasn't clobbered
    from long_term_memory import recall

    assert recall("fakesig").get("conclusions") is None


def test_gate_phase_recursion_limit_falls_back_to_clarifying_question(monkeypatch, tmp_path):
    handle_id = "fake_handle2"

    def fake_create_deep_agent(*, model, tools, system_prompt, backend, middleware=None):
        # Gate phase never manages to call submit_plan_tool before hitting
        # its own step limit.
        return _FakeGraph(steps=[_looping_tool_call_step()], raise_after=1)

    monkeypatch.setattr(agent, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(report, "OUTPUTS_DIR", tmp_path)

    result = agent.run(f"data/{handle_id}.csv", "does the gate phase loop forever?")

    assert result["status"] == "needs_clarification"
