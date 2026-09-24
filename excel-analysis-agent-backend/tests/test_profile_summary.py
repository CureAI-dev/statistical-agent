"""Unit tests for agent.py's _extract_profile_summary - the helper that
pulls profile_tool's own result back out of the gate phase's messages so
run() can hand phase 2 those facts instead of it re-calling profile_tool
(see the phase-2 duplicate-profile-call fix).
"""

import json

from langchain_core.messages import AIMessage, ToolMessage

from agent import _extract_profile_summary


def _profile_tool_message(payload: dict) -> ToolMessage:
    return ToolMessage(content=json.dumps(payload), name="profile_tool", tool_call_id="call_1")


def test_extract_profile_summary_keeps_only_the_essential_facts():
    payload = {
        "handle_id": "h",
        "n_rows": 50,
        "n_cols": 3,
        "dtypes": {"a": "int64", "b": "str", "c": "str"},
        "null_counts": {"a": 0, "b": 4, "c": 0},
        "numeric_summary": {"a": {"mean": 1.0}},  # not carried forward
        "sample_rows": [{"a": 1, "b": "x", "c": "y"}],  # not carried forward
    }
    result = _extract_profile_summary([_profile_tool_message(payload)])
    assert result == {
        "n_rows": 50,
        "n_cols": 3,
        "dtypes": {"a": "int64", "b": "str", "c": "str"},
        "columns_with_nulls": {"b": 4},  # zero-null columns dropped
    }


def test_extract_profile_summary_none_when_profile_tool_never_called():
    assert _extract_profile_summary([AIMessage(content="hello")]) is None


def test_extract_profile_summary_none_on_error_result():
    error_message = _profile_tool_message({"error": "No file loaded"})
    assert _extract_profile_summary([error_message]) is None


def test_extract_profile_summary_uses_the_last_call_if_called_twice():
    first = _profile_tool_message({"n_rows": 1, "n_cols": 1, "dtypes": {}, "null_counts": {}})
    second = _profile_tool_message({"n_rows": 99, "n_cols": 2, "dtypes": {}, "null_counts": {}})
    result = _extract_profile_summary([first, second])
    assert result["n_rows"] == 99
