"""Unit tests for agent.py's _data_dictionary - no LLM, no sandbox, no
network.

This function exists because of a specific, reproduced failure (see
results/test-2): the agent had SEX's real values from profile_tool, lost
them when summarization fired, assumed the column was coded 0/1 when it is
actually coded 1/2, filtered an empty group, and reported the resulting
NaN as "insufficient sample size" - a claim about the data that was really
a claim about its own guess. The dictionary is pinned into phase 2's
system prompt, which compaction cannot touch, so the real coding stays in
front of the model for the whole run.

The regression these tests lock in is therefore specific: a low-cardinality
column must show its ACTUAL VALUES, not merely its dtype. Knowing SEX is
int64 would not have prevented that bug; knowing it holds 1 and 2 would.
"""

import pandas as pd

from agent import _data_dictionary


def _line_for(dictionary: str, column: str) -> str:
    return next(ln for ln in dictionary.splitlines() if ln.startswith(f"- {column} "))


def test_low_cardinality_column_lists_its_real_values():
    """The exact results/test-2 regression: SEX coded 1/2, never 0/1."""
    df = pd.DataFrame({"SEX": [1] * 25 + [2] * 28})
    line = _line_for(_data_dictionary(df), "SEX")
    assert "'1': 25" in line and "'2': 28" in line
    # The wrong assumption that caused the bug must not be representable
    # as a value present in this column.
    assert "'0'" not in line


def test_identifier_like_column_is_summarized_not_listed():
    """53 unique IDs cost tokens and teach nothing - the pinned block is
    resent on every model call, so it has to stay small."""
    df = pd.DataFrame({"RESPONDENT_ID": [f"SIM-{i:04d}" for i in range(53)]})
    line = _line_for(_data_dictionary(df), "RESPONDENT_ID")
    assert "identifier-like" in line and "53 unique" in line
    assert "SIM-0000" not in line


def test_wide_numeric_column_reports_range_not_every_value():
    df = pd.DataFrame({"AGE": list(range(18, 75))})
    line = _line_for(_data_dictionary(df), "AGE")
    assert "min=18" in line and "max=74" in line


def test_missing_values_are_reported():
    """A column's null count changes whether a group comparison is even
    runnable, so it belongs in the same pinned block."""
    df = pd.DataFrame({"SCORE": [1.0, 2.0, None, None, 3.0]})
    assert "n_missing=2" in _line_for(_data_dictionary(df), "SCORE")


def test_high_cardinality_categorical_is_truncated_and_says_so():
    df = pd.DataFrame({"CITY": [f"city_{i}" for i in range(20)] * 2})
    line = _line_for(_data_dictionary(df), "CITY")
    assert "more)" in line


def test_every_column_appears_exactly_once():
    """The model reads this as the file's full column inventory - a column
    silently missing from it is a column it may not know exists."""
    df = pd.DataFrame({"A": [1, 2], "B": ["x", "y"], "C": [0.5, 0.75]})
    lines = _data_dictionary(df).splitlines()
    assert len(lines) == 3
    assert [ln.split(" ")[1] for ln in lines] == ["A", "B", "C"]
