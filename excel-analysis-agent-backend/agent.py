"""
The agent itself: an LLM that can look at a spreadsheet and answer
questions about it by calling tools, instead of us hardcoding the steps.

How it fits together:
- `tools.py` has the plain functions that actually touch data (read a file,
  compute stats on it). Those functions return the FULL pandas DataFrame.
- This file wraps those functions as "tools" the model is allowed to call,
  and makes sure the DataFrame itself never gets sent to the model — only a
  small summary plus an id ("handle") it can use to refer to that data in
  later tool calls. This keeps big spreadsheets from blowing up the model's
  context window (see FR-3.1 / FR-1.3 in docs/requirements.md).
- `create_agent` wires the model + tools into the actual loop: ask the
  model what to do -> run the tool it picked -> show it the result -> ask
  again -> ... -> until it just answers in plain text.
- `run_code_tool` lets the model run its own pandas code against the file,
  inside the isolated E2B sandbox from `sandbox_tool.py` (never on this
  machine). One sandbox is shared across every tool call in a single
  `run()`, so variables the model defines (like `df`) stay alive between
  calls - that's what lets it build up a multi-step analysis (recode ->
  reverse-score -> aggregate) instead of starting over each time.
- `recommend_test_tool` picks the right statistical test (t-test,
  chi-square, regression, ...) from a fixed lookup table in `tools.py`, not
  from the model's own judgment - see docs/requirements.md FR-9.9. The
  model still writes and runs the actual test with run_code_tool.
- `classify_columns_tool` tags every column as likert/categorical/
  open_ended/identifier/continuous, using real signals (unique-value
  counts, whether values match a common Likert wording) plus a suggested
  type - see FR-9.1/FR-9.3. The model can override any suggestion after
  reading the column itself. The result is stored in CLASSIFICATIONS so
  later steps (subscale grouping, scoring) can reuse it.
"""

import math
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sandbox_tool import Runtime
from tools import classify_columns, profile, read_excel, recommend_test

load_dotenv()

# Handle store: keeps the real, possibly-huge DataFrames in this process's
# memory, keyed by a short id string. The model only ever sees the id and a
# summary - never the actual rows - which is what "handle" means in the
# requirements doc.
HANDLES: dict = {}

# Committed column classification per handle_id (see classify_columns_tool
# below) - the place later milestones (grouping, scoring) will read "which
# columns are Likert items" from.
CLASSIFICATIONS: dict = {}

# The sandbox for this run, created the first time a tool needs it and
# closed at the end of run() - see _get_sandbox()/_close_sandbox() below.
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


def _close_sandbox() -> None:
    """Shut down the sandbox at the end of a run - E2B sandboxes cost money
    while they're alive, so this must not be skipped even if the agent
    loop raised an error."""
    global _sandbox
    if _sandbox is not None:
        _sandbox.close()
        _sandbox = None


def _truncate(text: str) -> str:
    """Cap tool output before it enters the model's context (FR-5.3) -
    without this, a long print() from the model's own code could blow the
    context budget."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    remaining = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:MAX_OUTPUT_CHARS]}\n... (truncated, {remaining} more characters)"


def _json_safe(value: Any) -> Any:
    """
    pandas/numpy stats (df.describe(), null counts, etc.) come back as
    numpy int64/float64, and sometimes NaN - none of which the model's
    tool-result JSON can carry as-is. Recursively convert everything to
    plain Python types so the result can be sent back to the model.

    Returns `Any` on purpose: this walks an arbitrary nested structure and
    the shape of what comes out mirrors whatever went in.
    """
    if isinstance(value, dict):
        return {key: _json_safe(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


@tool
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

    return {
        "handle_id": handle_id,
        "n_rows": loaded["n_rows"],
        "n_cols": loaded["n_cols"],
        "columns": loaded["columns"],
        "sandbox_path": sandbox_path,
    }


@tool
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
    return _json_safe(profile(handle))


@tool
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
    suggestions = _json_safe(classify_columns(handle))

    result = CLASSIFICATIONS.get(handle_id, suggestions)
    if overrides:
        for column, new_type in overrides.items():
            if column in result:
                result[column] = {**result[column], "suggested_type": new_type, "confidence": "overridden"}
    else:
        result = suggestions

    CLASSIFICATIONS[handle_id] = result
    return result


# The model that decides which tool to call and when. gpt-4o-mini is cheap
# and more than capable of this kind of tool-picking + summarizing task.
model = ChatOpenAI(model="gpt-4o-mini")

SYSTEM_PROMPT = (
    "You are a data-analysis agent for spreadsheets. Load a file with "
    "read_excel_tool, inspect it with profile_tool, and use run_code_tool "
    "for anything that needs actual computation. Inside run_code_tool, "
    "always load the file from the sandbox_path read_excel_tool gave you - "
    "the original file path does not exist inside the sandbox. "
    "Before running any statistical test (t-test, chi-square, correlation, "
    "regression, etc.), check whether the test needs a normality check "
    "(comparing groups or correlating variables does; predicting an "
    "outcome does not). If it does, run one for real with run_code_tool "
    "(e.g. scipy.stats.shapiro) before calling recommend_test_tool - never "
    "assume normality. Then call recommend_test_tool to get the correct "
    "test and function name - never pick one from memory. Run it with "
    "run_code_tool (scipy.stats or statsmodels.api are both available), "
    "and in your final answer state the test used, whether its assumptions "
    "held, the statistic, the p-value, and what it means in plain language. "
    "If the file looks like a questionnaire/survey (many columns with a "
    "small set of repeated response options), call classify_columns_tool "
    "before analyzing it. Review the suggested type and confidence for "
    "each column; override any you disagree with. In your final answer, "
    "report the classification (grouped by type) with confidence, and "
    "note any overrides with your reason."
)

# One call builds the whole "ask model -> run tool -> show result -> ask
# again" loop for us (this is the "ReAct" agent pattern).
agent_graph = create_agent(
    model,
    tools=[read_excel_tool, profile_tool, run_code_tool, recommend_test_tool, classify_columns_tool],
    system_prompt=SYSTEM_PROMPT,
)


def run(file_path: str, question: str) -> str:
    """Ask the agent to analyze `file_path` and answer `question`.

    Prints each step (tool calls, tool results, final answer) as it
    happens, so you can watch the agent's reasoning trace live. Returns the
    final plain-English answer.
    """
    user_message = f"Analyze the file at this path: {file_path}\n\nQuestion: {question}"

    final_answer = ""
    try:
        # Passed inline (not pulled into a variable first) so the type
        # checker matches this dict literal against the exact shape
        # create_agent expects, instead of just inferring "some dict".
        for step in agent_graph.stream(
            {"messages": [{"role": "user", "content": user_message}]},
            stream_mode="values",
        ):
            last_message = step["messages"][-1]
            last_message.pretty_print()

            is_final_answer = last_message.type == "ai" and not last_message.tool_calls
            if is_final_answer:
                final_answer = last_message.content
    finally:
        _close_sandbox()

    return final_answer
