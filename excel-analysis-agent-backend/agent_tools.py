"""
Tool wrappers around the plain functions in tools.py. This is the layer
that makes sure the model never sees a raw DataFrame (only a handle_id and
small summaries - see FR-3.1/FR-1.3 in docs/requirements.md), and that
data-processing code only ever runs inside the sandbox - E2B by
default (sandbox_tool.py), or a subprocess on this machine when no
E2B_API_KEY is available (local_runtime.py); see _pick_runtime_class.

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
latency requirement - and with @_with_retry underneath that, retrying an
unexpected exception with backoff before giving up and returning an
{"error": ...} dict instead of crashing the run - NFR-6.
"""

import functools
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from e2b_code_interpreter import NotEnoughSpaceException, RateLimitException, TimeoutException
from langchain_core.tools import tool

import long_term_memory
import study_plan
from sandbox_tool import Runtime
from skill_runtime import SkillDisclosureSession, load_skill, upload_scripts
from store import (
    CLASSIFICATIONS,
    GROUPS,
    HANDLES,
    SANDBOX_PATHS,
    SCALES,
    SCHEMA_SIGS,
    STUDY_PLAN,
    TOOL_CALLS,
    json_safe,
)
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
ACTIVE_SKILL = load_skill()
SKILL_DISCLOSURE = SkillDisclosureSession(ACTIVE_SKILL)

MAX_OUTPUT_CHARS = 2000

# NFR-6: retry with backoff on tool failure before giving up. Every tool
# below already returns an {"error": ...} dict for its own expected
# failure modes (missing handle_id, missing column, ...) instead of
# raising - an actual exception here means something unexpected happened
# (e.g. a transient E2B/network hiccup), which is exactly what's worth
# retrying.
MAX_TOOL_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1

# Only these are plausibly transient - worth spending backoff time on a
# retry that might succeed the second time. Everything else (a KeyError/
# TypeError from an actual bug in this file or tools.py, or an e2b error
# like InvalidArgumentException/AuthenticationException that reflects a bad
# call, not a flaky one) will fail identically on every attempt - retrying
# those just burns ~3s of backoff before returning the same failure, and
# makes a real bug look identical in the trace to a genuinely flaky
# external call, which is exactly what NFR-6 shouldn't be masking.
_RETRYABLE_EXCEPTIONS = (
    TimeoutException,  # e2b sandbox/request timeout
    RateLimitException,  # e2b API rate limit
    NotEnoughSpaceException,  # e2b sandbox disk pressure
    ConnectionError,
    TimeoutError,
    sqlite3.OperationalError,  # e.g. "database is locked" (long_term_memory)
)


def _pick_runtime_class():
    """Choose where the agent's code runs.

    E2B (a cloud sandbox) is the default and the only option that honours
    CLAUDE.md rule 2 - code the model writes never touches this machine.
    It needs E2B_API_KEY. Without one, every tool used to fail at the very
    first call (read_excel_tool uploads the file), taking down runs that
    never needed to execute code at all.

    SANDBOX_BACKEND controls this:
      "e2b"   - always E2B; fail loudly if the key is missing.
      "local" - always run on this machine.
      "auto"  - (default) E2B when a key is present, local otherwise.
    """
    backend = (os.getenv("SANDBOX_BACKEND") or "auto").strip().lower()
    has_key = bool((os.getenv("E2B_API_KEY") or "").strip())

    if backend == "e2b" or (backend == "auto" and has_key):
        return Runtime
    if backend not in ("auto", "local"):
        raise ValueError(
            f"SANDBOX_BACKEND={backend!r} is not one of 'e2b', 'local', 'auto'."
        )

    from local_runtime import LocalRuntime

    print(
        "\n*** Running code on THIS MACHINE, not in a cloud sandbox. ***\n"
        "    No E2B_API_KEY found (set one from https://e2b.dev/dashboard?tab=keys\n"
        "    to restore isolation, or set SANDBOX_BACKEND=e2b to fail instead of\n"
        "    falling back). Code the model writes can read and write anything this\n"
        "    user account can.\n",
        file=sys.stderr,
    )
    return LocalRuntime


def _get_sandbox():
    """Start the runtime on first use, then reuse the same one for the rest
    of this run so variables/state persist across run_code_tool calls."""
    global _sandbox
    if _sandbox is None:
        _sandbox = _pick_runtime_class()()
        # scipy ships with the E2B sandbox already; statsmodels (needed for
        # the two regression tests) doesn't, so install it once up front
        # rather than relying on the model to remember to. LocalRuntime
        # translates the %pip line into a real pip install in its own
        # interpreter, so this works on either backend.
        _sandbox.run_code("%pip install -q statsmodels")
        uploaded_scripts = upload_scripts(ACTIVE_SKILL, _sandbox)
        if uploaded_scripts:
            script_dir = str(Path(uploaded_scripts[0]).parent)
            _sandbox.run_code(
                "import sys\n"
                f"_skill_script_dir = {script_dir!r}\n"
                "if _skill_script_dir not in sys.path:\n"
                "    sys.path.insert(0, _skill_script_dir)"
            )
    return _sandbox


def close_sandbox() -> None:
    """Shut down the sandbox at the end of a run - E2B sandboxes cost money
    while they're alive, so this must not be skipped even if the agent
    loop raised an error. Called from agent.py's run()."""
    global _sandbox
    if _sandbox is not None:
        _sandbox.close()
        _sandbox = None
    SKILL_DISCLOSURE.reset()


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


def _with_retry(func):
    """Retry a transient-looking exception (_RETRYABLE_EXCEPTIONS) with
    exponential backoff (MAX_TOOL_RETRIES attempts, RETRY_BACKOFF_BASE_SECONDS
    * 2**attempt between them) before giving up. Applied under @_timed (so a
    retried call's full latency, backoff included, still lands in
    TOOL_CALLS) and over the raw function (so @tool still sees the original
    name/docstring/signature via functools.wraps).

    Any other exception is NOT retried (no point burning backoff time on a
    failure that will repeat identically), but is still caught here and
    turned into the same {"error": ...} dict shape, on the first attempt -
    this project's agent graphs register no ToolErrorMiddleware, and
    ToolNode's own default error handling only covers argument-binding/
    validation errors, not exceptions raised during a tool's actual
    execution (confirmed by reading langgraph/prebuilt/tool_node.py and
    langchain/agents/middleware/tool_error.py) - so letting a non-retryable
    exception propagate from here would crash the whole run, not just this
    tool call, which is the exact silent-crash NFR-6 rules out. The message
    is worded differently from the retried case ("not retried" vs. "failed
    after N attempts") so a real bug doesn't read identically to a flaky
    external call in the trace."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(MAX_TOOL_RETRIES):
            try:
                return func(*args, **kwargs)
            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt < MAX_TOOL_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
            except Exception as exc:  # noqa: BLE001 - deliberate: see docstring above.
                return {"error": f"{func.__name__} failed (not retried - non-transient): {exc!r}"}
        return {"error": f"{func.__name__} failed after {MAX_TOOL_RETRIES} attempts: {last_error}"}

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
@_with_retry
def read_excel_tool(path: str, sheet: str | int = 0) -> dict:
    """Load an Excel or CSV file so it can be analyzed.

    Use this first, before profiling, running code on, or asking questions
    about a file. Returns a summary (row/column counts, column names, a
    handle_id, and a sandbox_path) - not the actual data. Pass the
    handle_id to other tools to refer to this file without reloading it.

    The result also carries structural_issues, if any were found: blank
    rows are already dropped (no action needed), but merged_cells or
    possible_multi_row_header are only flagged, not fixed - read what's
    reported and decide how to handle it yourself (e.g. re-reading with a
    different header row via run_code_tool) before analyzing the file.

    Args:
        path: path to the .xlsx/.xls/.xlsm/.csv file.
        sheet: sheet name or index, for Excel files (ignored for CSV).
    """
    loaded = read_excel(path, sheet)
    handle_id = loaded["handle_id"]
    df = loaded["dataframe"]
    HANDLES[handle_id] = df

    # Upload into the sandbox so run_code_tool can load it directly - the
    # "working copy" the requirements doc calls for; the original file on
    # disk is never touched. Normally that's just the raw file bytes, but
    # if read_excel() dropped blank rows or fell back to a non-UTF-8
    # encoding, the host df no longer matches those raw bytes - upload the
    # cleaned df instead so run_code_tool doesn't see stale junk rows or
    # hit the same encoding error pandas already worked around.
    # Ask the runtime where uploads go: E2B can write to /home/user, this
    # machine can't, so LocalRuntime answers with its own temp workdir.
    sandbox_path = _get_sandbox().sandbox_path_for(f"{handle_id}{Path(path).suffix}")
    needs_reupload = loaded["structural_issues"].get("blank_rows_dropped") or (
        loaded.get("encoding_used") and loaded["encoding_used"] != "utf-8"
    )
    if needs_reupload and Path(path).suffix.lower() == ".csv":
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = tmp.name
        df.to_csv(tmp_path, index=False)
        _get_sandbox().upload_file(tmp_path, sandbox_path)
        Path(tmp_path).unlink(missing_ok=True)
    else:
        _get_sandbox().upload_file(path, sandbox_path)
    SANDBOX_PATHS[handle_id] = sandbox_path
    # Captured once, here, before score_items_tool can mutate this same df
    # in place (adds '{group}_score' columns) - see store.py's SCHEMA_SIGS
    # docstring for why every other long_term_memory call reuses this
    # instead of recomputing from HANDLES[handle_id].
    SCHEMA_SIGS[handle_id] = long_term_memory.schema_signature(df)

    result = {
        "handle_id": handle_id,
        "n_rows": loaded["n_rows"],
        "n_cols": loaded["n_cols"],
        "columns": loaded["columns"],
        "sandbox_path": sandbox_path,
        "structural_issues": loaded["structural_issues"],
    }
    if loaded.get("encoding_used") and loaded["encoding_used"] != "utf-8":
        result["encoding_used"] = loaded["encoding_used"]
    return result


@tool
@_timed
@_with_retry
def profile_tool(handle_id: str) -> dict:
    """Inspect a previously loaded file: column types, null counts, basic
    numeric stats, and one sample row.

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
@_with_retry
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
@_with_retry
def load_skill_resource_tool(relative_path: str) -> dict:
    """Load one level-3 resource from the active statistical skill.

    Use only when the active SKILL.md points to a specific reference,
    template, or resource needed for the current analysis stage. Never use
    this tool to load every skill file.

    Args:
        relative_path: Allowlisted path such as
            references/test_selection_guide.md or templates/oneway_anova.md.
    """
    return SKILL_DISCLOSURE.load(relative_path)


@tool
@_timed
@_with_retry
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
@_with_retry
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

    The result also includes low_variance_columns: every column where all
    non-null values are identical (n_unique <= 1) - a deterministic signal
    computed from the data, not a judgment call, same idea as
    score_items_tool's likely_reverse_coded_items. A column with no
    variance can't serve as a usable outcome or predictor for any
    statistical test - see SYSTEM_PROMPT for what to do if the task asks
    you to use one of these columns anyway.

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
        # Only overrides are worth remembering across sessions - a
        # mechanical suggestion is re-derivable any time from the same
        # data, but an override is the agent's own judgment call (FR-4.3).
        long_term_memory.remember_classification_overrides(SCHEMA_SIGS[handle_id], overrides)
    else:
        result = suggestions

    CLASSIFICATIONS[handle_id] = result

    # Computed from the already-committed `result`, not folded into it -
    # CLASSIFICATIONS must stay a flat {column: info} dict (group_items_tool
    # iterates it expecting every value to be a column's info), so this
    # extra field is added only to what goes back to the model, not to what
    # gets stored.
    low_variance_columns = [col for col, info in result.items() if info.get("n_unique", 2) <= 1]
    if low_variance_columns:
        return {**result, "low_variance_columns": low_variance_columns}
    return result


@tool
@_timed
@_with_retry
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
        # Reverse-coding is exactly the judgment call that drifts between
        # runs on this project (see docs/progress.md section 11) -
        # remembering it here means a later run against this same schema
        # can reuse a validated call instead of re-guessing from scratch.
        long_term_memory.remember_scale(SCHEMA_SIGS[handle_id], col, result)
    else:
        result = suggestion

    SCALES.setdefault(handle_id, {})[col] = result
    return result


@tool
@_timed
@_with_retry
def group_items_tool(
    handle_id: str,
    groups: dict[str, list[str]] | None = None,
    rationale: dict[str, str] | None = None,
    aggregation: dict[str, str] | None = None,
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
        aggregation: optional {subscale_name: "mean" | "sum"}, read by
            score_items_tool. Defaults to "mean" for any subscale not
            named. Use "sum" when the instrument's own scoring rule is a
            total, not a per-item average (e.g. PSS-10 reports a 0-40
            sum) - decide this the same way you decide reverse-coding, by
            reading the instrument's actual scoring rule, never guess.
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

    bad_aggregations = {
        name: agg for name, agg in (aggregation or {}).items() if agg not in ("mean", "sum")
    }
    if bad_aggregations:
        return {"error": f"aggregation must be 'mean' or 'sum', got {bad_aggregations}"}

    committed = {
        name: {
            "columns": cols,
            "rationale": (rationale or {}).get(name),
            "aggregation": (aggregation or {}).get(name, "mean"),
        }
        for name, cols in groups.items()
    }
    GROUPS[handle_id] = committed
    long_term_memory.remember_groups(SCHEMA_SIGS[handle_id], committed)
    return committed


@tool
@_timed
@_with_retry
def score_items_tool(handle_id: str, groups: list[str] | None = None) -> dict:
    """Compute each committed subscale's per-respondent score and
    Cronbach's alpha, and write the new score column(s) into the working
    data automatically.

    Requires group_items_tool to have committed groups first. Each group
    is scored as a mean or a sum, whichever group_items_tool's commit call
    set as that group's aggregation (mean if it wasn't set). Writes a
    '{group}_score' column per group into the file's data and re-uploads it
    to the sandbox at its existing path - reload the file in run_code_tool
    afterward (e.g. pd.read_csv(sandbox_path) again) to see the new
    column, then use it (not a hand-built average or total) in any later
    test.

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
    selected_aggregations = {
        name: info.get("aggregation", "mean") for name, info in committed_groups.items() if name in selected
    }

    handle = {"handle_id": handle_id, "dataframe": HANDLES[handle_id]}
    result = score_items(handle, selected, SCALES.get(handle_id, {}), selected_aggregations)

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


@tool
@_timed
@_with_retry
def recall_memory_tool(handle_id: str) -> dict:
    """Check whether this exact file schema (same column names and types)
    was already analyzed in a past run - possibly in a separate session,
    days ago (FR-4.3's long-term memory).

    Call this once, right after profile_tool, before classify_columns_tool.
    If seen_before is True, the result may include classifications
    (committed overrides only), scales (committed reverse-coding/point-
    scale calls, keyed by column), groups (committed subscale groupings),
    and conclusions (up to the last 5 prior run summaries for this
    schema) - all validated human judgment from a past run, not a
    mechanical suggestion. Treat these as a strong starting point: you
    can still call classify_columns_tool/infer_scale_tool/group_items_tool
    again to override anything that looks wrong for this file's actual
    values, but don't re-derive from scratch a call that was already made
    and committed for this exact schema. If seen_before is False, there's
    nothing to reuse - proceed normally.

    The result also includes any durable output preferences the user has
    stated in a past run (see set_preference_tool) - apply them without
    being asked again.

    Args:
        handle_id: the id returned by read_excel_tool.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}
    result = long_term_memory.recall(SCHEMA_SIGS[handle_id])
    preferences = long_term_memory.get_all_preferences()
    if preferences:
        result["preferences"] = preferences
    return result


@tool
@_timed
@_with_retry
def set_preference_tool(key: str, value: str) -> dict:
    """Remember a durable output preference the user states (e.g. "always
    round percentages to 1 decimal", "show subscale scores as a table,
    not prose") so future runs - including in a separate session - apply
    it without being told again.

    Only call this when the user actually states a preference in this
    conversation. Never invent one on your own.

    Args:
        key: a short name for the preference, e.g. "decimal_places".
        value: the preference itself, in plain text.
    """
    long_term_memory.set_preference(key, value)
    return {"remembered": {key: value}}


@tool
@_timed
@_with_retry
def submit_plan_tool(
    status: str,
    question: str | None = None,
    assumption: str | None = None,
    tasks: list[dict] | None = None,
) -> dict:
    """Call this exactly once, as your very last action, to end this gate
    phase. No other tool call should come after it.

    Call with status="needs_clarification" and a question - one specific,
    targeted question referencing this file's actual columns - if the
    request is ambiguous and you were told not to assume.

    Call with status="ready" and a task list otherwise. If you made an
    assumption instead of asking, state it in one sentence via
    `assumption`.

    Args:
        status: "needs_clarification" or "ready".
        question: the single clarifying question to ask. Required when
            status is "needs_clarification".
        assumption: one sentence stating an assumption you made instead
            of asking, if any. Only meaningful when status is "ready".
        tasks: the task list. Required when status is "ready". Each item:
            {"step": one of "classify"/"infer_scale"/"group"/"score"/
            "test"/"report", "description": str, "status": "pending"}.
            Omit steps that don't apply to this file or question - e.g.
            skip classify/infer_scale/group entirely for a file that's
            already fully pre-scored.
    """
    return {"received": True, "status": status}


@tool
@_timed
@_with_retry
def study_plan_tool(
    section: str,
    column: str = "",
    instrument: str = "",
    subscale: str = "",
) -> dict:
    """Read the study plan for this dataset: what each column means, how
    every derived variable is built, which items are reverse-scored, their
    exact answer options, and the analysis that was planned.

    Prefer this over working things out from the data. Anything the plan
    states is a fact about how the study was designed, not a guess from
    observed values - an item's reverse-coding and its anchor (0-4 vs 1-5)
    in particular cannot be recovered reliably from the numbers alone.

    Only fetch the section you need; the whole plan is far too large to
    read at once.

    Args:
        section: which slice to read.
            "overview"      - research question, hypothesis, design, N.
            "analysis"      - the tests the study planned, and why.
            "variables"     - the outcome and predictor names.
            "column_map"    - question wording -> coded column name. Use
                              this when the file's headers are question
                              text rather than codes.
            "item"          - one item's spec; needs `column`.
            "variable"      - one derived variable's definition (method,
                              source_items, range); needs `column`.
            "items"         - every item for an `instrument` or `subscale`.
            "reverse_coded" - the list of reverse-scored item columns.
        column: the column name, for section "item" or "variable".
        instrument: e.g. "PSS10", for section "items".
        subscale: e.g. "Perceived helplessness", for section "items".
    """
    plan = STUDY_PLAN.get("plan")
    if not plan:
        return {"error": "No study plan was supplied for this run - infer from the data as usual."}

    if section == "overview":
        return json_safe(study_plan.overview(plan))
    if section == "analysis":
        return json_safe(study_plan.planned_analysis(plan))
    if section == "variables":
        return json_safe(study_plan.target_variables(plan))
    if section == "column_map":
        return json_safe(study_plan.column_map(plan))
    if section == "reverse_coded":
        return {"reverse_coded_items": study_plan.reverse_coded_items(plan)}
    if section in ("item", "variable"):
        if not column:
            return {"error": f'section "{section}" needs a `column` argument.'}
        getter = study_plan.item_spec if section == "item" else study_plan.variable_spec
        found = getter(plan, column)
        if found is None:
            return {
                "error": f"{column!r} is not in the study plan.",
                "hint": "Call section='column_map' to see the plan's column names.",
            }
        return json_safe(found)
    if section == "items":
        if not (instrument or subscale):
            return {"error": 'section "items" needs an `instrument` or a `subscale`.'}
        found = study_plan.items_for(plan, instrument=instrument or None, subscale=subscale or None)
        if not found:
            return {"error": f"No items matched instrument={instrument!r} subscale={subscale!r}."}
        return json_safe({"items": found, "n": len(found)})

    return {
        "error": f"Unknown section {section!r}.",
        "valid_sections": [
            "overview", "analysis", "variables", "column_map",
            "item", "variable", "items", "reverse_coded",
        ],
    }


def _plan_likert_items(plan: dict) -> list[dict]:
    """Every questionnaire item the plan marks as a Likert response - the
    set infer_scales_tool commits when from_plan=True."""
    return [
        q
        for section in plan.get("questionnaire", {}).get("sections", [])
        for q in section.get("questions", [])
        if q.get("response_type") == "likert" and q.get("column")
    ]


@tool
@_timed
@_with_retry
def infer_scales_tool(
    handle_id: str,
    from_plan: bool = False,
    items: list[dict] | None = None,
    columns: list[str] | None = None,
) -> dict:
    """Commit the scales for MANY Likert items in one call. Use this instead
    of calling infer_scale_tool once per item.

    A survey has tens of Likert items and every separate tool call costs a
    whole model round trip with the full conversation resent. One real run
    spent 755,079 tokens getting 19 of 42 items scaled that way and was cut
    off by the token budget before any analysis ran; the same work in one
    call is a single round trip.

    Two ways to use it:

    - from_plan=True (preferred when a study plan is loaded): take every
      item's anchor, label->score map and reverse-coding straight from the
      plan. These are stated design facts, so nothing needs inferring and
      you do not have to retype them. Restrict to some items with `columns`.
    - items=[...]: commit your own judgments in bulk, one entry per item,
      e.g. [{"col": "PSS10_4", "reverse_coded": true,
              "label_to_score": {"Never": 0, ...}}, ...].

    Returns one line per item plus a `failed` list, so a bad column name
    does not lose the rest of the batch.

    Args:
        handle_id: the id returned by read_excel_tool.
        from_plan: commit every Likert item from the study plan.
        items: explicit per-item overrides (col, reverse_coded, label_to_score).
        columns: with from_plan, only these columns.
    """
    if handle_id not in HANDLES:
        return {"error": f"No file loaded with handle_id '{handle_id}'. Call read_excel_tool first."}
    if not from_plan and not items:
        return {"error": "Pass from_plan=True, or `items` with one entry per column."}

    if from_plan:
        plan = STUDY_PLAN.get("plan")
        if not plan:
            return {"error": "No study plan was supplied for this run - pass `items` instead."}
        wanted = set(columns) if columns else None
        # A plan names items by code (PSS10_4) but a raw survey export's
        # headers are the question wording, and only the plan knows they are
        # the same thing. Resolve each item to whichever of the two actually
        # exists in this dataframe, so from_plan works on both shapes -
        # without this every item fails as "not a column in this file".
        present = set(HANDLES[handle_id].columns)
        items = []
        for question in _plan_likert_items(plan):
            spec = study_plan.item_spec(plan, question["column"])
            if not spec:
                continue
            if wanted is not None and spec["column"] not in wanted:
                continue
            text = (spec.get("text") or "").strip()
            actual = spec["column"] if spec["column"] in present else (text if text in present else None)
            if actual is None:
                continue
            items.append(
                {
                    "col": actual,
                    "reverse_coded": spec.get("reverse_coded"),
                    "label_to_score": spec.get("label_to_score") or None,
                    "plan_column": spec["column"],
                }
            )
        if not items:
            return {
                "error": "None of the study plan's Likert items match a column in this file.",
                "hint": "Check study_plan_tool(section='column_map') against the file's real headers.",
            }

    df = HANDLES[handle_id]
    committed: dict[str, dict] = {}
    failed: list[dict] = []
    for entry in items:
        col = entry.get("col")
        if not col:
            failed.append({"item": entry, "reason": "no 'col' key"})
            continue
        if col not in df.columns:
            failed.append({"col": col, "reason": "not a column in this file"})
            continue
        handle = {"handle_id": handle_id, "dataframe": df}
        result = {**json_safe(infer_scale(handle, col))}
        reverse_coded = entry.get("reverse_coded")
        label_to_score = entry.get("label_to_score")
        if reverse_coded is not None:
            result["reverse_coded"] = reverse_coded
        if label_to_score:
            result["label_to_score"] = label_to_score
            result["n_points"] = len(label_to_score)
        if reverse_coded is not None or label_to_score:
            result["confidence"] = "overridden"
            long_term_memory.remember_scale(SCHEMA_SIGS[handle_id], col, result)
        SCALES.setdefault(handle_id, {})[col] = result
        committed[col] = {
            "plan_column": entry.get("plan_column"),
            "n_points": result.get("n_points"),
            "reverse_coded": result.get("reverse_coded"),
            "confidence": result.get("confidence"),
        }

    return {
        "committed": committed,
        "n_committed": len(committed),
        "failed": failed,
        "note": (
            "Scales are committed. Full label->score maps are not echoed back "
            "to keep this result small - they are stored and used by "
            "score_items_tool."
        ),
    }
