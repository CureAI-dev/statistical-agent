"""Tests for agent_tools._with_retry's transient-vs-not distinction
(P0-3, see docs/improvement-plan.md). No LLM, no sandbox, no network -
these exercise the decorator directly against fake functions.

Before this fix, EVERY exception was retried MAX_TOOL_RETRIES times, which
is unhelpful for a real bug (KeyError/TypeError) - it fails identically on
every attempt, so retrying just burns backoff time and makes it
indistinguishable from a genuinely flaky call in the trace. After the fix,
only exceptions that are plausibly transient get retried; everything else
fails fast, on the first attempt, with a message that says so.
"""

from e2b_code_interpreter import InvalidArgumentException, TimeoutException

import agent_tools
from agent_tools import MAX_TOOL_RETRIES, _with_retry


def test_with_retry_retries_transient_exception(monkeypatch):
    monkeypatch.setattr(agent_tools.time, "sleep", lambda seconds: None)
    calls = []

    def flaky():
        calls.append(1)
        raise TimeoutException("sandbox timed out")

    result = _with_retry(flaky)()

    assert len(calls) == MAX_TOOL_RETRIES
    assert f"failed after {MAX_TOOL_RETRIES} attempts" in result["error"]


def test_with_retry_succeeds_after_a_transient_failure(monkeypatch):
    monkeypatch.setattr(agent_tools.time, "sleep", lambda seconds: None)
    calls = []

    def eventually_ok():
        calls.append(1)
        if len(calls) < 2:
            raise TimeoutException("sandbox timed out")
        return {"ok": True}

    result = _with_retry(eventually_ok)()

    assert result == {"ok": True}
    assert len(calls) == 2


def test_with_retry_does_not_retry_a_programming_error(monkeypatch):
    monkeypatch.setattr(agent_tools.time, "sleep", lambda seconds: None)
    calls = []

    def buggy():
        calls.append(1)
        raise KeyError("some_missing_key")

    result = _with_retry(buggy)()

    # Not retried - fails on the very first attempt, so the error is
    # distinguishable from a genuinely transient one in the trace.
    assert len(calls) == 1
    assert "not retried - non-transient" in result["error"]


def test_with_retry_does_not_retry_a_non_transient_e2b_error(monkeypatch):
    monkeypatch.setattr(agent_tools.time, "sleep", lambda seconds: None)
    calls = []

    def bad_call():
        calls.append(1)
        raise InvalidArgumentException("bad argument")

    result = _with_retry(bad_call)()

    assert len(calls) == 1
    assert "not retried - non-transient" in result["error"]
