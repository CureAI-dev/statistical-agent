"""Unit tests for the optional analysis brief - no LLM, no network.

The brief lets a caller pin down input/output variables and an analysis
plan up front. Its defining property is that it is *optional at every
level*: no brief at all, or a brief filling in only one field, must both
leave the agent's own inference in charge of everything else. These tests
exist mostly to keep that property from eroding into a required schema.

The column check is deliberately deterministic and runs before the first
LLM call: naming a column the file does not have is the one ambiguity that
needs no judgment to spot, and the failure it prevents is the expensive
one - a paid run that confidently analyses the wrong column.
"""

import json

import pytest

from agent import _format_brief, load_brief, validate_brief

COLUMNS = ["RESPONDENT_ID", "AGE", "SEX", "PSS10_1", "PSS10_total_score"]


# --- load_brief ------------------------------------------------------

def test_no_path_means_no_brief():
    assert load_brief(None) is None
    assert load_brief("") is None


def test_loads_a_partial_brief(tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"outcome": "SEX"}))
    assert load_brief(str(f)) == {"outcome": "SEX"}


def test_unknown_field_is_rejected_by_name(tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"outcome": "SEX", "covariates": ["AGE"]}))
    with pytest.raises(ValueError, match="covariates"):
        load_brief(str(f))


def test_non_object_json_is_rejected(tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(["SEX"]))
    with pytest.raises(TypeError, match="JSON object"):
        load_brief(str(f))


# --- validate_brief --------------------------------------------------

def test_none_and_empty_briefs_always_validate():
    validate_brief(None, COLUMNS)
    validate_brief({}, COLUMNS)


def test_real_columns_validate():
    validate_brief({"outcome": "SEX", "predictors": ["AGE", "PSS10_1"]}, COLUMNS)


def test_a_bare_string_is_accepted_where_a_list_is_meant():
    """`predictors: "AGE"` is the obvious way to write one predictor."""
    validate_brief({"predictors": "AGE"}, COLUMNS)


def test_unknown_column_is_rejected_and_named():
    with pytest.raises(ValueError, match="SEXX"):
        validate_brief({"outcome": "SEXX"}, COLUMNS)


def test_near_miss_gets_a_suggestion():
    """The realistic typo is a near-miss, not an invented name."""
    with pytest.raises(ValueError, match="Did you mean.*PSS10_total_score"):
        validate_brief({"outcome": "PSS10_totl_score"}, COLUMNS)


def test_free_text_fields_are_never_column_checked():
    """analysis_plan and notes are prose - mentioning a column name that
    does not exist there is not an error."""
    validate_brief({"analysis_plan": "compare NOT_A_COLUMN across groups"}, COLUMNS)


# --- _format_brief ---------------------------------------------------

def test_absent_brief_formats_to_empty_string():
    """Callers concatenate this unconditionally, so 'no brief' has to be
    the empty string rather than None or a placeholder."""
    assert _format_brief(None) == ""
    assert _format_brief({}) == ""


def test_only_populated_fields_appear():
    text = _format_brief({"outcome": "SEX", "predictors": [], "notes": "  "})
    assert "outcome: SEX" in text
    assert "predictors" not in text
    assert "notes" not in text


def test_every_field_round_trips():
    text = _format_brief({
        "outcome": "PSS10_1",
        "predictors": ["AGE", "SEX"],
        "analysis_plan": "Spearman, then Mann-Whitney by SEX",
        "notes": "PSS10 is 0-4 anchored",
    })
    assert "outcome: PSS10_1" in text
    assert "predictors: AGE, SEX" in text
    assert "Spearman, then Mann-Whitney by SEX" in text
    assert "0-4 anchored" in text
