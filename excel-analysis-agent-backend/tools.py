import pandas as pd
from pathlib import Path


def read_excel(path: str, sheet: str | int = 0) -> dict:
    file_path = Path(path)
    extension = file_path.suffix.lower()

    if extension == ".csv":
        df = pd.read_csv(file_path)
    elif extension in (".xlsx", ".xls", ".xlsm"):
        df = pd.read_excel(file_path, sheet_name=sheet)
    else:
        raise ValueError(f"Unsupported file extension: {extension}")

    return {
        "handle_id": file_path.stem,
        "dataframe": df,
        "n_rows": len(df),
        "n_cols": len(df.columns),
        "columns": df.columns.tolist(),
    }


def profile(handle: dict) -> dict:
    df = handle["dataframe"]

    return {
        "handle_id": handle["handle_id"],
        "n_rows": len(df),
        "n_cols": len(df.columns),
        "dtypes": df.dtypes.astype(str).to_dict(),
        "null_counts": df.isnull().sum().to_dict(),
        "numeric_summary": df.describe().to_dict(),
        "sample_rows": df.head(3).to_dict(orient="records"),
    }


# Deterministic lookup implementing the client's test-selection table
# (docs/requirements.md FR-9.9). Plain code, no LLM judgment involved, so
# the choice of test can never be misremembered or hallucinated - only the
# inputs (variable types, normality, group count) require judgment, and
# those come from the agent inspecting the actual data first.
_TEST_TABLE = {
    ("compare_groups", 2, True): ("Independent t-test", "scipy.stats.ttest_ind"),
    ("compare_groups", 2, False): ("Mann-Whitney U test", "scipy.stats.mannwhitneyu"),
    ("compare_groups", "many", True): ("One-way ANOVA", "scipy.stats.f_oneway"),
    ("compare_groups", "many", False): ("Kruskal-Wallis test", "scipy.stats.kruskal"),
    ("association", False): ("Chi-square test", "scipy.stats.chi2_contingency"),
    ("association", True): ("Fisher's exact test", "scipy.stats.fisher_exact"),
    ("correlation", True): ("Pearson correlation", "scipy.stats.pearsonr"),
    ("correlation", False): ("Spearman correlation", "scipy.stats.spearmanr"),
    ("predict_binary",): ("Binary logistic regression", "statsmodels.api.Logit"),
    ("predict_continuous",): ("Linear regression", "statsmodels.api.OLS"),
}


def recommend_test(
    question_type: str,
    is_normal: bool = True,
    n_groups: int = 2,
    small_expected_counts: bool = False,
) -> dict:
    """Look up which statistical test fits a research question.

    question_type: one of "compare_groups", "association", "correlation",
        "predict_binary", "predict_continuous".
    is_normal: whether the outcome variable is normally distributed
        (relevant for compare_groups/correlation only).
    n_groups: how many groups are being compared (relevant for
        compare_groups only).
    small_expected_counts: whether a categorical crosstab has any expected
        cell count under 5 (relevant for association only).
    """
    if question_type == "compare_groups":
        group_key = 2 if n_groups <= 2 else "many"
        key = (question_type, group_key, is_normal)
    elif question_type == "association":
        key = (question_type, small_expected_counts)
    elif question_type == "correlation":
        key = (question_type, is_normal)
    else:
        key = (question_type,)

    if key not in _TEST_TABLE:
        raise ValueError(f"No test mapping for question_type={question_type!r}")

    test, function = _TEST_TABLE[key]
    return {"test": test, "function": function, "why": f"Matches '{question_type}' in the test-selection table."}


# Common Likert response wordings (docs/requirements.md FR-9.1/FR-9.2),
# ordered low -> high so the same list doubles as the label->score map in
# infer_scale() below. A column whose values are a subset of one of these
# is almost certainly a Likert item, not a plain nominal category - this
# is what separates "likert" from "categorical" in _classify_column,
# since both can have a handful of short string values.
_LIKERT_SCALES = [
    ("never", "almost never", "sometimes", "fairly often", "very often"),
    ("never", "rarely", "sometimes", "often", "always"),
    ("strongly disagree", "disagree", "neutral", "agree", "strongly agree"),
    ("strongly disagree", "disagree", "neither agree nor disagree", "agree", "strongly agree"),
    (
        "strongly disagree", "disagree", "somewhat disagree",
        "neither agree nor disagree", "somewhat agree", "agree", "strongly agree",
    ),
    ("poor", "fair", "good", "excellent"),
]

_IDENTIFIER_KEYWORDS = ("timestamp", "consent", "respondent id", "identifier")


def _classify_column(series: pd.Series, n_rows: int) -> dict:
    non_null = series.dropna()
    n_unique = non_null.nunique()
    unique_ratio = n_unique / n_rows if n_rows else 0.0
    signals = {
        "dtype": str(series.dtype),
        "n_unique": n_unique,
        "unique_ratio": round(unique_ratio, 3),
        "top_values": non_null.value_counts().head(8).to_dict(),
    }

    if pd.api.types.is_numeric_dtype(series):
        return {**signals, "suggested_type": "continuous", "confidence": "medium"}

    values = {str(v).strip().lower() for v in non_null.unique()}
    if len(values) >= 2 and any(values <= set(scale) for scale in _LIKERT_SCALES):
        return {**signals, "suggested_type": "likert", "confidence": "high"}

    column_name = series.name.lower() if isinstance(series.name, str) else ""
    if any(keyword in column_name for keyword in _IDENTIFIER_KEYWORDS):
        return {**signals, "suggested_type": "identifier", "confidence": "medium"}

    avg_length = non_null.astype(str).str.len().mean() if len(non_null) else 0
    if unique_ratio > 0.5 and avg_length > 25:
        return {**signals, "suggested_type": "open_ended", "confidence": "medium"}

    if n_unique <= 10:
        return {**signals, "suggested_type": "categorical", "confidence": "medium"}

    return {**signals, "suggested_type": "categorical", "confidence": "low"}


def classify_columns(handle: dict) -> dict:
    """Suggest a type for every column: likert, categorical, open_ended,
    identifier, or continuous, with a confidence and the signals behind
    the guess. The caller (the agent) may override any suggestion after
    reading the column name/values itself - this only proposes a default.
    """
    df = handle["dataframe"]
    n_rows = len(df)
    return {col: _classify_column(df[col], n_rows) for col in df.columns}


def infer_scale(handle: dict, col: str) -> dict:
    """Suggest the point scale for one Likert item (docs/requirements.md
    FR-9.2): how many points, the label->score map (low to high), and
    whether it's reverse-coded, with a confidence and the signals behind
    the guess. The caller (the agent) may override - this only proposes a
    default, same as classify_columns.

    Reverse-coding can't be told from a column's own values alone (it
    depends on which way the item points relative to the construct it
    belongs to, e.g. "felt confident" vs "felt nervous" in a stress
    scale) - this always suggests False and leaves that judgment to the
    caller, who can read the item's wording against its subscale.
    """
    series = handle["dataframe"][col]
    non_null = series.dropna()

    if pd.api.types.is_numeric_dtype(series):
        points = sorted(non_null.unique().tolist())
        return {
            "n_points": len(points),
            "label_to_score": {str(p): p for p in points},
            "reverse_coded": False,
            "confidence": "low",
            "note": "Values are already numeric; assumed already on a score scale in ascending order.",
        }

    values_present = {str(v).strip().lower() for v in non_null.unique()}
    for scale in _LIKERT_SCALES:
        if values_present <= set(scale):
            return {
                "n_points": len(scale),
                "label_to_score": {label: i + 1 for i, label in enumerate(scale)},
                "reverse_coded": False,
                "confidence": "high",
                "note": "Matched a known Likert wording; label order treated as low-to-high.",
            }

    return {
        "n_points": len(values_present),
        "label_to_score": None,
        "reverse_coded": None,
        "confidence": "low",
        "note": f"Unrecognized wording {sorted(values_present)}; decide the label order yourself after reading the item.",
    }
