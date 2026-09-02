"""Unit tests for tools.py's plain functions - no LLM, no sandbox, no
network. These are the functions every agent tool wraps (see
agent_tools.py), so a bug here is a bug the agent inherits silently.

Written as part of the 2026-09 architecture review (see
docs/improvement-plan.md) - before this file, every one of these functions
was only ever exercised by a real, paid LLM run (see docs/progress.md).
"""

import numpy as np
import pandas as pd
import pytest

import tools
from tools import (
    _cronbachs_alpha,
    _item_total_correlations,
    classify_columns,
    group_items,
    infer_scale,
    read_excel,
    recommend_test,
    score_items,
)

# ---------------------------------------------------------------------------
# recommend_test (FR-9.9) - a plain lookup table, so this just locks the
# table down against an accidental edit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_test"),
    [
        ({"question_type": "compare_groups", "is_normal": True, "n_groups": 2}, "Independent t-test"),
        ({"question_type": "compare_groups", "is_normal": False, "n_groups": 2}, "Mann-Whitney U test"),
        ({"question_type": "compare_groups", "is_normal": True, "n_groups": 3}, "One-way ANOVA"),
        ({"question_type": "compare_groups", "is_normal": False, "n_groups": 3}, "Kruskal-Wallis test"),
        ({"question_type": "association", "small_expected_counts": False}, "Chi-square test"),
        ({"question_type": "association", "small_expected_counts": True}, "Fisher's exact test"),
        ({"question_type": "correlation", "is_normal": True}, "Pearson correlation"),
        ({"question_type": "correlation", "is_normal": False}, "Spearman correlation"),
        ({"question_type": "predict_binary"}, "Binary logistic regression"),
        ({"question_type": "predict_continuous"}, "Linear regression"),
    ],
)
def test_recommend_test_table(kwargs, expected_test):
    assert recommend_test(**kwargs)["test"] == expected_test


def test_recommend_test_unknown_question_type_raises():
    with pytest.raises(ValueError):
        recommend_test("not_a_real_question_type")


# ---------------------------------------------------------------------------
# classify_columns (FR-9.1)
# ---------------------------------------------------------------------------


def test_classify_columns_tags_each_real_signal():
    n = 20
    df = pd.DataFrame(
        {
            "Respondent ID": [f"R{i}" for i in range(n)],
            "Age": list(range(20, 40)),
            "Mood": (["Never", "Rarely", "Sometimes", "Often", "Always"] * 4),
            "Comments": [
                f"This is a fairly long open-ended comment number {i} about the survey experience overall."
                for i in range(n)
            ],
            "Region": (["North", "South"] * 10),
        }
    )
    result = classify_columns({"handle_id": "h", "dataframe": df})

    assert result["Respondent ID"]["suggested_type"] == "identifier"
    assert result["Age"]["suggested_type"] == "continuous"
    assert result["Mood"]["suggested_type"] == "likert"
    assert result["Comments"]["suggested_type"] == "open_ended"
    assert result["Region"]["suggested_type"] == "categorical"


def test_classify_columns_reports_n_unique_for_low_variance_detection():
    # agent_tools.classify_columns_tool derives low_variance_columns from
    # n_unique <= 1 on this result - lock down that the signal is present.
    df = pd.DataFrame({"constant": ["No"] * 10, "varies": list(range(10))})
    result = classify_columns({"handle_id": "h", "dataframe": df})
    assert result["constant"]["n_unique"] == 1


# ---------------------------------------------------------------------------
# infer_scale (FR-9.2) - never guesses reverse-coding; that's asserted here
# by checking it's always False/None regardless of input.
# ---------------------------------------------------------------------------


def test_infer_scale_numeric_column():
    df = pd.DataFrame({"q": [1, 2, 3, 4, 5]})
    result = infer_scale({"handle_id": "h", "dataframe": df}, "q")
    assert result["confidence"] == "low"
    assert result["reverse_coded"] is False
    assert result["label_to_score"] == {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5}


def test_infer_scale_known_wording():
    df = pd.DataFrame({"q": ["Never", "Sometimes", "Always", "Often", "Rarely"]})
    result = infer_scale({"handle_id": "h", "dataframe": df}, "q")
    assert result["confidence"] == "high"
    assert result["reverse_coded"] is False
    assert result["label_to_score"]["never"] == 1
    assert result["label_to_score"]["always"] == 5


def test_infer_scale_unrecognized_wording_leaves_judgment_to_caller():
    df = pd.DataFrame({"q": ["Red", "Blue", "Green"]})
    result = infer_scale({"handle_id": "h", "dataframe": df}, "q")
    assert result["confidence"] == "low"
    assert result["label_to_score"] is None
    assert result["reverse_coded"] is None


# ---------------------------------------------------------------------------
# group_items (FR-9.4) - correlation signal + skip columns without a
# committed scale.
# ---------------------------------------------------------------------------


def test_group_items_skips_columns_without_committed_scale():
    df = pd.DataFrame({"q1": [1, 2, 3, 4, 5], "q2": [1, 2, 3, 4, 5], "q3": [5, 4, 3, 2, 1]})
    # A non-empty label_to_score is what group_items treats as "committed" -
    # an empty dict is falsy in Python, so it reads the same as "missing".
    scale = {"n_points": 5, "reverse_coded": False, "label_to_score": {"1": 1}}
    scales = {"q1": scale, "q2": scale}  # q3 deliberately has no committed scale

    result = group_items({"handle_id": "h", "dataframe": df}, ["q1", "q2", "q3"], scales)

    assert result["skipped"] == ["q3"]
    assert result["correlation"]["q1"]["q2"] == 1.0


# ---------------------------------------------------------------------------
# _cronbachs_alpha / _item_total_correlations edge cases - the guard
# clauses added by the 2026-09 missing-data fix (P0-1, see
# docs/improvement-plan.md). Both must return a clean None instead of a
# silent NaN when there isn't enough data to compute a real statistic.
# ---------------------------------------------------------------------------


def test_cronbachs_alpha_none_for_too_few_items():
    single_item = pd.DataFrame({"q1": [1.0, 2.0, 3.0, 4.0, 5.0]})
    assert _cronbachs_alpha(single_item) is None


def test_cronbachs_alpha_none_for_too_few_rows():
    one_row = pd.DataFrame({"q1": [1.0], "q2": [2.0]})
    assert _cronbachs_alpha(one_row) is None

    zero_rows = pd.DataFrame({"q1": pd.Series(dtype=float), "q2": pd.Series(dtype=float)})
    assert _cronbachs_alpha(zero_rows) is None


def test_item_total_correlations_none_for_too_few_rows():
    one_row = pd.DataFrame({"q1": [1.0], "q2": [2.0]})
    result = _item_total_correlations(one_row)
    assert result == {"q1": None, "q2": None}


# ---------------------------------------------------------------------------
# score_items (FR-9.5/FR-9.6) - the P0-1 regression test. Before this fix,
# Cronbach's alpha and item_total_correlations were computed on ALL
# respondents including ones missing an item in the group; pandas' default
# skip-NaN sum/var silently treated a missing item as a 0 contribution to
# that respondent's total, biasing both statistics. This proves the fix:
# the reliability numbers now come from complete cases only, are reported
# as differing from what the old (buggy) full-data computation would have
# given, and the incompleteness itself is surfaced via
# n_incomplete_respondents rather than staying invisible.
# ---------------------------------------------------------------------------


def test_score_items_alpha_uses_complete_cases_only():
    df = pd.DataFrame(
        {
            "q1": [1, 2, 3, 4, 5],
            "q2": [2, 3, 4, 5, 1],
            "q3": [1, 2, np.nan, 4, 5],  # respondent index 2 skipped this item
        }
    )
    scale = {"n_points": 5, "reverse_coded": False, "label_to_score": {}}
    scales = {"q1": scale, "q2": scale, "q3": scale}
    groups = {"g": ["q1", "q2", "q3"]}

    result = score_items({"handle_id": "h", "dataframe": df}, groups, scales)
    summary = result["summary"]["g"]

    item_scores = df[["q1", "q2", "q3"]].astype(float)
    complete = item_scores.dropna()
    alpha_if_incomplete_rows_were_wrongly_included = _cronbachs_alpha(item_scores)
    alpha_complete_cases_only = _cronbachs_alpha(complete)

    assert summary["n_respondents"] == 5
    assert summary["n_incomplete_respondents"] == 1
    assert summary["cronbachs_alpha"] == alpha_complete_cases_only
    # The two numbers must differ for this test to actually prove anything -
    # if they were equal, the complete-cases restriction wouldn't be doing
    # anything observable for this data.
    assert alpha_complete_cases_only != alpha_if_incomplete_rows_were_wrongly_included

    # The per-respondent subscale score itself is intentionally left as a
    # prorated (skip-NaN) mean - a legitimate scoring convention, not what
    # this fix changed. Respondent index 2 answered q1=3, q2=4, q3 missing.
    assert result["new_columns"]["g_score"].iloc[2] == pytest.approx(3.5)


def test_score_items_reverse_coding_applied():
    df = pd.DataFrame({"q1": [1, 2, 3, 4, 5]})
    scale = {"n_points": 5, "reverse_coded": True, "label_to_score": {}}
    result = score_items({"handle_id": "h", "dataframe": df}, {"g": ["q1"]}, {"q1": scale})
    # reverse-coded: (n_points + 1) - raw = 6 - raw
    assert result["new_columns"]["g_score"].tolist() == [5, 4, 3, 2, 1]


# ---------------------------------------------------------------------------
# read_excel (FR-1.1/FR-1.4/FR-1.5)
# ---------------------------------------------------------------------------


def test_read_excel_csv_basic(tmp_path):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("a,b\n1,2\n3,4\n")

    loaded = read_excel(str(csv_path))

    assert loaded["handle_id"] == "sample"
    assert loaded["n_rows"] == 2
    assert loaded["columns"] == ["a", "b"]
    assert loaded["structural_issues"] == {}


def test_read_excel_drops_blank_rows(tmp_path):
    csv_path = tmp_path / "with_blank.csv"
    csv_path.write_text("a,b\n1,2\n,\n3,4\n")

    loaded = read_excel(str(csv_path))

    assert loaded["n_rows"] == 2
    assert loaded["structural_issues"]["blank_rows_dropped"] == 1


def test_read_excel_latin1_fallback(tmp_path):
    csv_path = tmp_path / "latin1.csv"
    # "café" encoded as latin-1: the trailing 0xE9 byte for "é" is not a
    # valid standalone UTF-8 sequence, so the utf-8 attempt must fail and
    # fall back.
    csv_path.write_bytes("name\ncafé\n".encode("latin-1"))

    loaded = read_excel(str(csv_path))

    assert loaded["encoding_used"] == "latin-1"
    assert loaded["dataframe"]["name"].tolist() == ["café"]


def test_read_excel_chunked_path_reads_all_rows(tmp_path, monkeypatch):
    # Force the chunked path (FR-1.4) on an otherwise-tiny file by dropping
    # the size threshold to well below its real byte count.
    csv_path = tmp_path / "chunked.csv"
    rows = [f"{i},{i * 2}" for i in range(500)]
    csv_path.write_text("a,b\n" + "\n".join(rows) + "\n")

    monkeypatch.setattr(tools, "LARGE_FILE_BYTES", 10)
    monkeypatch.setattr(tools, "CHUNK_ROWS", 50)

    loaded = read_excel(str(csv_path))

    assert loaded["n_rows"] == 500
    assert loaded["dataframe"]["a"].iloc[-1] == 499


def test_read_excel_unsupported_extension_raises(tmp_path):
    bad_path = tmp_path / "notes.txt"
    bad_path.write_text("not a spreadsheet")

    with pytest.raises(ValueError, match="Unsupported file extension"):
        read_excel(str(bad_path))


def test_read_excel_oversized_excel_rejected(tmp_path, monkeypatch):
    xlsx_path = tmp_path / "big.xlsx"
    pd.DataFrame({"a": [1, 2]}).to_excel(xlsx_path, index=False)

    monkeypatch.setattr(tools, "LARGE_FILE_BYTES", 1)

    with pytest.raises(ValueError, match="Convert it to CSV"):
        read_excel(str(xlsx_path))


def test_read_excel_detects_merged_cells(tmp_path):
    from openpyxl import Workbook

    xlsx_path = tmp_path / "merged.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Header"
    sheet["B1"] = "Value"
    sheet.merge_cells("A1:B1")
    sheet["A2"] = "row"
    sheet["B2"] = 1
    workbook.save(xlsx_path)

    loaded = read_excel(str(xlsx_path))

    assert "merged_cells" in loaded["structural_issues"]
