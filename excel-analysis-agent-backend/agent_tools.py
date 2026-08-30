"""
Tool wrappers around the plain functions in tools.py. This is the layer
that makes sure the model never sees a raw DataFrame (only a handle_id and
small summaries - see FR-3.1/FR-1.3 in docs/requirements.md), and that
data-processing code only ever runs inside the E2B sandbox
(sandbox_tool.py), never on this machine.

Suggest-then-commit tools (classify_columns_tool, infer_scale_tool,
group_items_tool) all follow the same shape: call once to see a suggestion
based on real signals, call again with your own judgment to override and
commit it. Later steps read the committed result (from store.py) instead
of re-deriving it or asking the model to remember its own past decision.

score_items_tool is the one tool that mutates the working data: once it
computes a subscale score, it writes the new column into the in-memory
dataframe and re-uploads it to the sandbox at the same path, so the next
run_code_tool call picks it up just by re-reading the CSV.

Every tool below is wrapped with @_timed (applied under @tool, so it
times the plain function before @tool turns it into a schema) recording
each call's wall-clock time into store.TOOL_CALLS - NFR-5's per-tool
latency requirement.
"""

import functools
import tempfile
import time
from pathlib import Path

from langchain_core.tools import tool

from sandbox_tool import Runtime
from store import CLASSIFICATIONS, GROUPS, HANDLES, SANDBOX_PATHS, SCALES, TOOL_CALLS, json_safe
from tools import (
    classify_columns,
    group_items,
    infer_scale,
    profile,
    read_excel,
    recommend_test,
    score_items,
)

# The sandbox for this run, created the first time a tool needs it and
# closed at the end of run() in agent.py.
_sandbox: Runtime | None = None

MAX_OUTPUT_CHARS = 2000


def _get_sandbox() -> Runtime:
    """Start the sandbox on first use, then reuse the same one for the rest
    of this run so variables/state persist across run_code_tool calls."""
    global _sandbox
    if _sandbox is None:
        _sandbox = Runtime()
        # scipy ships with the sandbox already; statsmodels (needed for the
        # two regression tests) doesn't, so install it once up front rather
        # than relying on the model to remember to.
        _sandbox.run_code("%pip install -q statsmodels")
    return _sandbox


def close_sandbox() -> None:
    """Shut down the sandbox at the end of a run - E2B sandboxes cost money
    while they're alive, so this must not be skipped even if the agent
    loop raised an error. Called from agent.py's run()."""
    global _sandbox
    if _sandbox is not None:
        _sandbox.close()
        _sandbox = None


def _timed(func):
    """Record each tool call's wall-clock time into store.TOOL_CALLS
    (NFR-5's "tool latency" requirement). Applied under @tool (not above
    it) so it wraps the plain function @tool sees - functools.wraps keeps
    the name/docstring/signature intact, which is what @tool reads to
    build the schema the model sees; wrapping the other way around would
    just time the already-built StructuredTool object instead."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            TOOL_CALLS.append({"tool": func.__name__, "seconds": round(time.perf_counter() - start, 3)})

    return wrapper


def _truncate(text: str) -> str:
    """Cap tool output before it enters the model's context (FR-5.3) -
    without this, a long print() from the model's own code could blow the
    context budget."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    remaining = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:MAX_OUTPUT_CHARS]}\n... (truncated, {remaining} more characters)"


@tool
@_timed
def read_excel_tool(path: str, sheet: str | int = 0) -> dict:
    """Load an Excel or CSV file so it can be analyzed.

    Use this first, before profiling, running code on, or asking questions
    about a file. Returns a summary (row/column counts, column names, a
    handle_id, and a sandbox_path) - not the actual data. Pass the
    handle_id to other tools to refer to this file without reloading it.

    Args:
        path: path to the .xlsx/.xls/.xlsm/.csv file.
        sheet: sheet name or index, for Excel files (ignored for CSV).
    """
    loaded = read_excel(path, sheet)
    handle_id = loaded["handle_id"]
    HANDLES[handle_id] = loaded["dataframe"]

    # Also upload the file into the sandbox so run_code_tool can load it
    # directly - this uploaded copy is the "working copy" the requirements
    # doc calls for; the original file on disk is never touched.
    sandbox_path = f"/home/user/{handle_id}{Path(path).suffix}"
    _get_sandbox().upload_file(path, sandbox_path)
    SANDBOX_PATHS[handle_id] = sandbox_path

    return {
        "handle_id": handle_id,
        "n_rows": loaded["n_rows"],
        "n_cols": loaded["n_cols"],
        "columns": loaded["columns"],
        "sandbox_path": sandbox_path,
    }


@tool
@_timed
def profile_tool(handle_id: str) -> dict:
    """Inspect a previously loaded file: column types, null counts, basic
    numeric stats, and a few sample rows.

    Use this to understand a file's shape and quality before analyzing it.

    Args:
        handle_id: the id returned by read_excel_tool.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}

    handle = {"handle_id": handle_id, "dataframe": HANDLES[handle_id]}
    return json_safe(profile(handle))


@tool
@_timed
def run_code_tool(code: str) -> dict:
    """Run Python code in a sandbox to compute, filter, or transform data -
    pandas and numpy are available. Use this for anything profile_tool
    doesn't already answer (percentages, filters, group-by's, custom
    calculations).

    Load the file with the sandbox_path from read_excel_tool (e.g.
    pd.read_csv(sandbox_path)), not the original path - the sandbox can't
    see the original file. Variables you define persist between calls to
    this tool, so build an analysis step by step instead of redoing
    everything each time; print() whatever you want to see back.

    Args:
        code: Python code to execute.
    """
    result = _get_sandbox().run_code(code)
    return {
        "stdout": _truncate(result["stdout"]),
        "result": _truncate(result["result"]) if result["result"] else None,
        "stderr": _truncate(result["stderr"]),
        "error": result["error"],
    }


@tool
@_timed
def recommend_test_tool(
    question_type: str,
    is_normal: bool = True,
    n_groups: int = 2,
    small_expected_counts: bool = False,
) -> dict:
    """Look up which statistical test fits a research question, per the
    standard test-selection table. Call this before running any
    statistical test - don't pick a test from memory.

    REQUIRED before calling this for compare_groups/correlation: run an
    actual normality check (e.g. scipy.stats.shapiro) via run_code_tool on
    the real data and pass its result as is_normal. Do not rely on the
    default - defaults exist only for question_types where the argument is
    unused, not as a stand-in for a real check.

    Args:
        question_type: one of "compare_groups" (comparing an outcome
            across 2+ groups), "association" (two categorical variables),
            "correlation" (two continuous/ordinal variables),
            "predict_binary" (predicting a yes/no outcome), or
            "predict_continuous" (predicting a numeric outcome).
        is_normal: is the outcome variable normally distributed? Only
            matters for compare_groups/correlation.
        n_groups: how many groups are being compared. Only matters for
            compare_groups.
        small_expected_counts: does a categorical crosstab have any
            expected cell count under 5? Only matters for association.
    """
    return recommend_test(question_type, is_normal, n_groups, small_expected_counts)


@tool
@_timed
def classify_columns_tool(handle_id: str, overrides: dict[str, str] | None = None) -> dict:
    """Classify every column in a loaded file as likert, categorical,
    open_ended, identifier, or continuous.

    Call once with no overrides to see a suggested type + confidence per
    column, based on real signals (unique-value counts, top values,
    whether the values match a common Likert wording like Never/Sometimes/
    Often/Always). If you disagree with a suggestion after reading the
    column name/values yourself (e.g. an unfamiliar ordinal scale the
    signals didn't recognize), call again with overrides for just those
    columns. Every call commits the result as this file's classification -
    later steps (grouping items into subscales, scoring) read from it.

    Args:
        handle_id: the id returned by read_excel_tool.
        overrides: optional {column_name: type} to replace suggested
            types. type must be one of likert, categorical, open_ended,
            identifier, continuous.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}

    handle = {"handle_id": handle_id, "dataframe": HANDLES[handle_id]}
    suggestions = json_safe(classify_columns(handle))

    result = CLASSIFICATIONS.get(handle_id, suggestions)
    if overrides:
        for column, new_type in overrides.items():
            if column in result:
                result[column] = {**result[column], "suggested_type": new_type, "confidence": "overridden"}
    else:
        result = suggestions

    CLASSIFICATIONS[handle_id] = result
    return result


@tool
@_timed
def infer_scale_tool(
    handle_id: str,
    col: str,
    reverse_coded: bool | None = None,
    label_to_score: dict[str, int] | None = None,
) -> dict:
    """Infer (or override) the point scale for one Likert item: how many
    points, the label->score map, and whether it's reverse-coded.

    Call once per Likert item (columns classify_columns_tool tagged
    "likert") with no overrides to see a suggested scale from that item's
    actual response values. Reverse-coding can't be inferred from the
    values alone - it depends on how the item's wording points relative to
    the construct it belongs to (e.g. "felt confident" is the reverse of
    "felt nervous" in a stress scale) - so read the item and call again
    with reverse_coded=True/False once you've judged it; do the same with
    label_to_score if the wording wasn't recognized (confidence "low",
    label_to_score None) or you disagree with the suggested order. Every
    call commits the result as this item's scale for later scoring steps.

    Args:
        handle_id: the id returned by read_excel_tool.
        col: the column name of the Likert item.
        reverse_coded: override whether this item is reverse-scored.
        label_to_score: override the label->score mapping, e.g.
            {"never": 1, "sometimes": 2, "always": 3}.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}
    df = HANDLES[handle_id]
    if col not in df.columns:
        return {"error": f"No column '{col}' in handle '{handle_id}'."}

    handle = {"handle_id": handle_id, "dataframe": df}
    suggestion = json_safe(infer_scale(handle, col))

    result = SCALES.get(handle_id, {}).get(col, suggestion)
    if reverse_coded is not None or label_to_score is not None:
        result = {**result}
        if reverse_coded is not None:
            result["reverse_coded"] = reverse_coded
        if label_to_score is not None:
            result["label_to_score"] = label_to_score
            result["n_points"] = len(label_to_score)
        result["confidence"] = "overridden"
    else:
        result = suggestion

    SCALES.setdefault(handle_id, {})[col] = result
    return result


@tool
@_timed
def group_items_tool(
    handle_id: str,
    groups: dict[str, list[str]] | None = None,
    rationale: dict[str, str] | None = None,
) -> dict:
    """Suggest, or commit, how this file's Likert items group into
    subscales/constructs.

    Call once with no `groups` to see a correlation matrix across every
    Likert item that already has a committed scale (from infer_scale_tool)
    - a signal for which items might move together, not a decision.
    Correlation can't say why items belong together or what to call the
    group, so read each item's actual wording yourself and decide, then
    call again with groups={"subscale name": ["item column", ...], ...}
    (optionally rationale={"subscale name": "why"}) to commit it. Every
    item you name must already have a committed scale.

    Args:
        handle_id: the id returned by read_excel_tool.
        groups: {subscale_name: [column, ...]} to commit a grouping.
            Omit to just see the correlation signal.
        rationale: optional {subscale_name: reason}, stored alongside the
            grouping for the record.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}

    scales = SCALES.get(handle_id, {})

    if groups is None:
        likert_cols = [
            col
            for col, info in CLASSIFICATIONS.get(handle_id, {}).items()
            if info.get("suggested_type") == "likert"
        ]
        if not likert_cols:
            return {"error": f"No Likert columns classified for '{handle_id}'. Call classify_columns_tool first."}
        handle = {"handle_id": handle_id, "dataframe": HANDLES[handle_id]}
        return json_safe(group_items(handle, likert_cols, scales))

    missing = sorted(
        {
            col
            for cols in groups.values()
            for col in cols
            if not scales.get(col) or not scales[col].get("label_to_score") or scales[col].get("reverse_coded") is None
        }
    )
    if missing:
        return {"error": f"These columns need a committed scale first (call infer_scale_tool): {missing}"}

    committed = {
        name: {"columns": cols, "rationale": (rationale or {}).get(name)} for name, cols in groups.items()
    }
    GROUPS[handle_id] = committed
    return committed


@tool
@_timed
def score_items_tool(handle_id: str, groups: list[str] | None = None) -> dict:
    """Compute each committed subscale's per-respondent score and
    Cronbach's alpha, and write the new score column(s) into the working
    data automatically.

    Requires group_items_tool to have committed groups first. Writes a
    '{group}_score' column per group into the file's data and re-uploads it
    to the sandbox at its existing path - reload the file in run_code_tool
    afterward (e.g. pd.read_csv(sandbox_path) again) to see the new
    column, then use it (not a hand-built average) in any later test.

    Args:
        handle_id: the id returned by read_excel_tool.
        groups: which committed subscale names to score. Omit to score
            every committed group.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}

    committed_groups = GROUPS.get(handle_id)
    if not committed_groups:
        return {"error": f"No groups committed for '{handle_id}'. Call group_items_tool first."}

    selected = {
        name: info["columns"] for name, info in committed_groups.items() if groups is None or name in groups
    }
    if not selected:
        return {"error": f"No matching committed groups for {groups}; committed groups are {list(committed_groups)}."}

    handle = {"handle_id": handle_id, "dataframe": HANDLES[handle_id]}
    result = score_items(handle, selected, SCALES.get(handle_id, {}))

    df = HANDLES[handle_id]
    for col_name, series in result["new_columns"].items():
        df[col_name] = series

    sandbox_path = SANDBOX_PATHS.get(handle_id)
    if sandbox_path:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = tmp.name
        df.to_csv(tmp_path, index=False)
        _get_sandbox().upload_file(tmp_path, sandbox_path)
        Path(tmp_path).unlink(missing_ok=True)

    return json_safe(result["summary"])
