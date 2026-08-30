import pandas as pd
from pathlib import Path

# Above this size (docs/requirements.md FR-1.4), CSV reads switch from one
# shot to chunked so peak memory grows with CHUNK_ROWS at a time instead of
# the whole file at once. Plain constants, not env-configurable yet - bump
# these directly if a real file needs a different cutoff.
LARGE_FILE_BYTES = 50_000_000
CHUNK_ROWS = 50_000


def _read_csv(file_path: Path, chunked: bool) -> tuple[pd.DataFrame, str]:
    """Read a CSV, trying UTF-8 then Latin-1 (FR-1.5's encoding-problem
    case). Returns the dataframe and whichever encoding actually worked."""
    for encoding in ("utf-8", "latin-1"):
        try:
            if chunked:
                chunks = pd.read_csv(file_path, encoding=encoding, chunksize=CHUNK_ROWS)
                return pd.concat(chunks, ignore_index=True), encoding
            return pd.read_csv(file_path, encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {file_path} as utf-8 or latin-1.")


def _detect_structural_issues(file_path: Path, df: pd.DataFrame) -> dict:
    """Detect structural problems (FR-1.5). Blank rows are dropped outright
    since that needs no judgment call. Merged cells and a possible
    multi-row header (Excel only) are only reported, never auto-fixed -
    deciding how to handle those needs the same kind of judgment as
    reverse-coding (see infer_scale), so it's left to the caller."""
    issues = {}

    blank_rows = df.index[df.isnull().all(axis=1)]
    if len(blank_rows):
        df.drop(blank_rows, inplace=True)
        df.reset_index(drop=True, inplace=True)
        issues["blank_rows_dropped"] = len(blank_rows)

    if file_path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        # read_only=True would be cheaper, but openpyxl's read-only
        # worksheet drops merged_cells entirely - not an option here since
        # detecting merges is the point. Fine memory-wise: oversized Excel
        # files are already rejected above before we get this far.
        workbook = load_workbook(file_path, read_only=False)
        merged = [str(cell_range) for cell_range in workbook.active.merged_cells.ranges]
        workbook.close()
        if merged:
            issues["merged_cells"] = merged

        # A blank header cell (typical when only the left cell of a merged
        # group carries text) comes back from pandas as "Unnamed: N" - a
        # reliable tell that row 1 wasn't really the full header and a
        # second header row got read as data instead.
        unnamed = sum(1 for col in df.columns if str(col).startswith("Unnamed:"))
        if len(df.columns) and unnamed > len(df.columns) / 2:
            issues["possible_multi_row_header"] = (
                f"{unnamed}/{len(df.columns)} columns have no real header ('Unnamed: N') - "
                "this file may actually have two header rows. Check it before analyzing."
            )

    return issues


def read_excel(path: str, sheet: str | int = 0) -> dict:
    file_path = Path(path)
    extension = file_path.suffix.lower()
    is_large = file_path.stat().st_size > LARGE_FILE_BYTES
    encoding_used = None

    if extension == ".csv":
        df, encoding_used = _read_csv(file_path, chunked=is_large)
    elif extension in (".xlsx", ".xls", ".xlsm"):
        if is_large:
            raise ValueError(
                f"{file_path.name} is over {LARGE_FILE_BYTES // 1_000_000}MB - pandas "
                "can't stream-read Excel. Convert it to CSV first."
            )
        df = pd.read_excel(file_path, sheet_name=sheet)
    else:
        raise ValueError(f"Unsupported file extension: {extension}")

    structural_issues = _detect_structural_issues(file_path, df)

    result = {
        "handle_id": file_path.stem,
        "dataframe": df,
        "n_rows": len(df),
        "n_cols": len(df.columns),
        "columns": df.columns.tolist(),
        "structural_issues": structural_issues,
    }
    if encoding_used:
        result["encoding_used"] = encoding_used
    return result


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


def _score_series(series: pd.Series, scale: dict) -> pd.Series:
    """Map one Likert item's raw responses to numeric scores using its
    committed scale (from infer_scale), then reverse them if flagged.
    Shared by group_items (needs numbers to correlate) and score_items
    (needs numbers to average into a subscale)."""
    if pd.api.types.is_numeric_dtype(series):
        scored = series.astype(float)
    else:
        keyed = {str(k).strip().lower(): v for k, v in scale["label_to_score"].items()}
        scored = series.astype(str).str.strip().str.lower().map(keyed)
    if scale.get("reverse_coded"):
        scored = (scale["n_points"] + 1) - scored
    return scored


def group_items(handle: dict, cols: list[str], scales: dict[str, dict]) -> dict:
    """Suggest which Likert items might share a subscale, using their
    pairwise correlation as a signal (docs/requirements.md FR-9.4).

    Correlation can say two items move together; it can't say why, or what
    to call the group they'd form - that needs reading the items' actual
    wording, which is left entirely to the caller (same division of labor
    as infer_scale's reverse_coded). This only computes a correlation
    matrix over `cols`, using each item's already-committed entry in
    `scales` (from infer_scale) to convert responses to numbers first.
    Columns without a usable committed scale are skipped and reported back.
    """
    df = handle["dataframe"]
    numeric = pd.DataFrame(index=df.index)
    skipped = []
    for col in cols:
        scale = scales.get(col)
        if not scale or not scale.get("label_to_score") or scale.get("reverse_coded") is None:
            skipped.append(col)
            continue
        numeric[col] = _score_series(df[col], scale)

    result = {
        "correlation": numeric.corr().round(2).to_dict(),
        "note": (
            "Signal only: items that correlate strongly are candidates for "
            "the same subscale, but only you can judge whether they share "
            "a construct by reading their wording. Call group_items_tool "
            "again with `groups` to commit your decision."
        ),
    }
    if skipped:
        result["skipped"] = skipped
        result["skipped_reason"] = "No usable committed scale yet - call infer_scale_tool for these first."
    return result


def _cronbachs_alpha(item_scores: pd.DataFrame) -> float | None:
    """Standard Cronbach's alpha: how well a group of items hangs together
    as one scale, from k/(k-1) * (1 - sum of item variances / variance of
    the summed score). Undefined for fewer than two items."""
    k = item_scores.shape[1]
    if k < 2:
        return None
    item_variance_sum = item_scores.var(axis=0, ddof=1).sum()
    total_variance = item_scores.sum(axis=1).var(ddof=1)
    if total_variance == 0:
        return None
    return round(float((k / (k - 1)) * (1 - item_variance_sum / total_variance)), 3)


def score_items(handle: dict, groups: dict[str, list[str]], scales: dict[str, dict]) -> dict:
    """Compute a per-respondent subscale score (mean of each item's
    reverse-coded score) and Cronbach's alpha for each group
    (docs/requirements.md FR-9.5/FR-9.6).

    Returns the new score column per group (as real pandas Series, for the
    caller to write into the working dataframe) plus summary stats and
    reliability - never the full per-respondent list, to keep the model's
    context small.
    """
    df = handle["dataframe"]
    new_columns: dict[str, pd.Series] = {}
    summary: dict[str, dict] = {}

    for group_name, cols in groups.items():
        item_scores = pd.DataFrame({col: _score_series(df[col], scales[col]) for col in cols})
        subscale_score = item_scores.mean(axis=1)
        score_column = f"{group_name}_score"
        new_columns[score_column] = subscale_score
        summary[group_name] = {
            # Spelled out explicitly (not left for the caller to
            # reconstruct from group_name) so the model can't typo or
            # forget the "_score" suffix when it goes to use this column.
            "score_column": score_column,
            "n_items": len(cols),
            "columns": cols,
            "cronbachs_alpha": _cronbachs_alpha(item_scores),
            "mean": float(subscale_score.mean()),
            "std": float(subscale_score.std()),
        }

    return {"new_columns": new_columns, "summary": summary}
