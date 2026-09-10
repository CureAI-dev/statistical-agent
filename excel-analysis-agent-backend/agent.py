"""
The agent itself: an LLM that can look at a spreadsheet and answer
questions about it by calling tools, instead of us hardcoding the steps.

- `tools.py` has the plain functions that actually touch data.
- `agent_tools.py` wraps those functions as tools the model is allowed to
  call, keeping the raw DataFrame out of the model's context (see its own
  module docstring for the tool-by-tool breakdown) and running any code
  the model writes inside the E2B sandbox (`sandbox_tool.py`), never here.
- `store.py` holds the committed state each tool reads/writes (loaded
  files, column classifications, Likert scales, subscale groupings) - one
  process, one run.
- `long_term_memory.py` holds the same kind of committed state, except it
  survives across separate `uv run` invocations (FR-4.3) - keyed by a
  file's schema signature, not handle_id, so it still recognizes a file
  that's been renamed or re-exported since the last time it was seen.
- `prompts.py` holds the system prompt.
- `report.py` writes each run's report/trace/results to disk once it's
  done (FR-8) - before this, they only ever printed to the terminal.
- This file just wires the model + tools into `create_deep_agent`'s loop
  (ask the model what to do -> run the tool it picked -> show it the
  result -> ask again -> ... -> until it just answers in plain text) and
  runs it, printing the trace and token usage as it goes.
  `create_deep_agent` (from the `deepagents` package) is the same kind of
  ReAct tool-calling loop `create_agent` gave us, plus a built-in
  summarization middleware that compresses old messages out of context
  once the conversation gets long - fixes runs on big files ballooning to
  1-1.2M tokens with no compression at all.
"""

import difflib
import json
import os
from functools import partial
from pathlib import Path

import pandas as pd
from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents._models import get_model_provider
from deepagents.backends import StateBackend
from deepagents.middleware import SummarizationMiddleware
from dotenv import load_dotenv
from langchain_core.messages.utils import count_tokens_approximately
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from langgraph.errors import GraphRecursionError

import long_term_memory
import study_plan as study_plan_module
from agent_tools import (
    classify_columns_tool,
    close_sandbox,
    group_items_tool,
    infer_scale_tool,
    infer_scales_tool,
    profile_tool,
    read_excel_tool,
    recall_memory_tool,
    recommend_test_tool,
    run_code_tool,
    score_items_tool,
    set_preference_tool,
    study_plan_tool,
    submit_plan_tool,
)
from prompts import GATE_SYSTEM_PROMPT, SYSTEM_PROMPT
from report import write_outputs
from store import HANDLES, SANDBOX_PATHS, SCHEMA_SIGS, STUDY_PLAN, TOOL_CALLS

load_dotenv()

# FR-7.3 / NFR-6: an explicit ceiling well below langgraph's own default
# (DEFAULT_RECURSION_LIMIT = 10007, confirmed by reading
# langgraph/_internal/_config.py - effectively unbounded for this project's
# runs). Without this, nothing stops a stuck-in-a-loop run short of the
# model eventually giving up on its own - docs/progress.md documents a real
# 297-call infer_scale_tool retry loop and a separate 90-call one, neither
# of which the library's own default ever caught. And even if that default
# were ever reached, nothing here handled GraphRecursionError, so the run
# would crash instead of "halt, summarize progress, return partial results"
# as FR-7.3 requires - confirmed by reading langgraph/errors.py, this is a
# real RecursionError subclass, not something LangGraph swallows on its
# own. Set generously above the largest legitimate run observed so far (101
# LLM turns / 108 tool calls, docs/progress.md's FR-2.3 re-verification) so
# a real, working run is never cut short by this.
MAX_GATE_STEPS = 30
MAX_PHASE2_STEPS = 200

# A hard ceiling on tokens for one whole run (gate phase + phase 2), the
# cost-shaped sibling of the step ceilings above. The step limits only
# catch a loop that keeps *calling tools*; they do nothing about a run
# whose individual calls simply get huge, and MAX_PHASE2_STEPS=200 is far
# too generous to be a cost guardrail on its own - a measured run reached
# 1,832,344 tokens in 83 turns (re-calling profile_tool/classify_columns_
# tool from scratch each time compaction made it forget it had already run
# them) and would have been allowed 117 turns more.
#
# 750,000 sits above what a healthy run needs - the largest completing run
# measured is well under this, and a full uncompacted gpt-5 run projects to
# roughly 535k - while stopping that 1.83M loop at about 40% of the way in.
# Override with MAX_RUN_TOKENS to tighten it for cheap exploratory runs;
# set it high only if a genuinely large file needs the room.
MAX_RUN_TOKENS = int(os.getenv("MAX_RUN_TOKENS") or 750_000)

_FALLBACK_QUESTION = (
    "Your request needs more detail before this file can be analyzed - "
    "please restate what outcome, comparison, or grouping you want, "
    "referencing the file's actual column names."
)


def _validate_plan_decision(decision: dict | None, handle_id: str) -> dict:
    """Turn the gate phase's captured submit_plan_tool arguments into a
    clean decision, defaulting to a clarifying question whenever the
    model's call was missing, malformed, or never happened at all - fail
    toward asking rather than silently guessing (see the design doc's
    Error handling section)."""
    if decision is None:
        return {"status": "needs_clarification", "question": _FALLBACK_QUESTION}

    status = decision.get("status")
    if status == "needs_clarification" and decision.get("question"):
        return {"status": "needs_clarification", "question": decision["question"]}
    if (
        status == "ready"
        and decision.get("tasks")
        and all(isinstance(t, dict) for t in decision["tasks"])
    ):
        return {
            "status": "ready",
            "handle_id": handle_id,
            "assumption": decision.get("assumption"),
            "tasks": decision["tasks"],
        }
    return {"status": "needs_clarification", "question": _FALLBACK_QUESTION}


# Every field of an analysis brief is optional, and so is the brief itself -
# the agent's own inference is the default, not a fallback. A brief only
# ever *narrows* what it would otherwise decide for itself: name an outcome
# and it stops weighing candidates, name nothing and nothing changes.
BRIEF_FIELDS = ("outcome", "predictors", "analysis_plan", "notes")

# Which brief fields name real columns, and so can be checked against the
# file before a single token is spent.
_BRIEF_COLUMN_FIELDS = ("outcome", "predictors")


def _as_list(value) -> list[str]:
    """Accept a bare string where a list is meant - `outcome: "PSS_score"`
    is the obvious way to write one outcome, and rejecting it would be
    pedantry."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def load_brief(path: str | None) -> dict | None:
    """Read an analysis brief from a JSON file. None means "no brief" -
    the agent decides everything itself, exactly as before this existed."""
    if not path:
        return None
    brief = json.loads(Path(path).read_text())
    if not isinstance(brief, dict):
        raise TypeError(f"{path}: an analysis brief must be a JSON object, got {type(brief).__name__}.")
    unknown = set(brief) - set(BRIEF_FIELDS)
    if unknown:
        raise ValueError(
            f"{path}: unknown field(s) {sorted(unknown)}. Valid fields are {list(BRIEF_FIELDS)}."
        )
    return brief


def validate_brief(brief: dict | None, columns: list[str]) -> None:
    """Check every column a brief names actually exists, and say which are
    wrong if not.

    Deliberately deterministic and done up front, before the gate phase's
    first LLM call: a typo'd outcome column is the one kind of ambiguity
    that needs no judgment to detect, and finding out about it after a
    paid run - or worse, having the model quietly analyze a different
    column - is the failure this prevents. Near-misses are suggested
    because the realistic mistake is 'PSS10_score' for
    'PSS10_total_score', not a wholly invented name."""
    if not brief:
        return
    # A study plan's derived variables (PSS10_TOTAL, PSQI_COMP_*) are
    # legitimate names for an outcome or predictor even though no such
    # column exists in the CSV: they do not exist until they are computed,
    # which is precisely what the plan describes. Without this, a brief
    # taken straight from a study plan is rejected in full.
    plan = STUDY_PLAN.get("plan")
    known = set(columns) | (study_plan_module.declared_columns(plan) if plan else set())
    problems = []
    for field in _BRIEF_COLUMN_FIELDS:
        for name in _as_list(brief.get(field)):
            if name in known:
                continue
            close = difflib.get_close_matches(name, columns, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            problems.append(f"  {field}: {name!r} is not a column in this file.{hint}")
    if problems:
        raise ValueError(
            "The analysis brief names columns the file does not have:\n"
            + "\n".join(problems)
            + "\n\nNote that subscale score columns (e.g. 'PSS10_total_score') do not "
            "exist until score_items_tool creates them - name the raw items, or "
            "describe the score you want in analysis_plan instead."
        )


def _format_brief(brief: dict | None) -> str:
    """Render a brief for the prompts. Empty string when there is none, so
    every caller can concatenate it unconditionally."""
    if not brief:
        return ""
    lines = []
    for field in ("outcome", "predictors"):
        values = _as_list(brief.get(field))
        if values:
            lines.append(f"- {field}: {', '.join(values)}")
    for field in ("analysis_plan", "notes"):
        value = (brief.get(field) or "").strip()
        if value:
            lines.append(f"- {field}: {value}")
    return "\n".join(lines)


def _data_dictionary(df, max_levels: int = 12) -> str:
    """Render every column's REAL values, for pinning into phase 2's system
    prompt.

    Same reasoning as handle_id/sandbox_path already being pinned there (see
    phase2_system_prompt): a fact left only in message history can be
    summarized away, and this project has now been bitten by that three
    times - the task list, then sandbox_path (the model guessed a
    plausible-looking but wrong "/sandbox/..." path), and then a column's
    coding. In results/test-2 the agent had SEX's real values from
    profile_tool, lost them to compaction event #5, wrote "# Assuming SEX is
    coded as 0 and 1" against a column actually coded {1: 25, 2: 28},
    filtered an empty group, got NaN back, and reported that NaN as
    "insufficient sample size" - blaming the data for its own guess.

    Value counts, not just dtypes: knowing SEX is int64 would not have
    prevented that; knowing it holds 1 and 2 would. Identifier-like columns
    are summarized rather than listed - 53 unique respondent IDs cost
    tokens and teach nothing.
    """
    lines = []
    for col in df.columns:
        values = df[col].dropna()
        n_missing = int(df[col].isna().sum())
        suffix = f", n_missing={n_missing}" if n_missing else ""
        n_unique = values.nunique()

        if n_unique <= max_levels:
            # Few enough values to list outright - the case that matters
            # most, since this is where a coding scheme lives (SEX: 1/2).
            counts = {str(k): int(v) for k, v in values.value_counts().items()}
            lines.append(f"- {col} [{values.dtype}]: {counts}{suffix}")
        elif values.dtype.kind in "ifu":
            # Numeric is checked before identifier-like: a continuous
            # measure can easily have every value distinct (age in days, a
            # score), and calling that an identifier would throw away the
            # range - which is the useful part - to state something false.
            lines.append(f"- {col} [{values.dtype}]: numeric, min={values.min()}, max={values.max()}{suffix}")
        elif len(values) and n_unique > 0.9 * len(values):
            lines.append(f"- {col} [{values.dtype}]: identifier-like, {n_unique} unique values{suffix}")
        else:
            counts = {str(k): int(v) for k, v in values.value_counts().head(max_levels).items()}
            lines.append(f"- {col} [{values.dtype}]: {counts} (+{n_unique - max_levels} more){suffix}")
    return "\n".join(lines)


def _format_tasks(tasks: list[dict]) -> str:
    """Render the committed task list as plain text for phase 2's opening
    message."""
    return "\n".join(
        f"- [{task.get('status', 'pending')}] {task.get('step')}: {task.get('description', '')}"
        for task in tasks
    )


# The model that decides which tool to call and when. gpt-5 is a reasoning
# model - it works through a problem before answering, which is aimed
# squarely at this project's one documented weak spot: the per-item
# judgment calls (reverse-coding, subscale grouping) that gpt-4o-mini made
# inconsistently run to run, and its habit of reporting a bad Cronbach's
# alpha instead of looping back to fix the item that caused it.
#
# NFR-1 (reproducibility) has to be handled differently than it was on
# gpt-4o-mini. That model took temperature=0; gpt-5 REJECTS it outright -
# HTTP 400, "Unsupported value: 'temperature' does not support 0 with this
# model. Only the default (1) value is supported" (verified against the
# live API, not assumed). So temperature is gone and `seed` takes its
# place - OpenAI's best-effort determinism knob, which gpt-5 does accept.
# Neither is a guarantee: temperature=0 never was either (OpenAI's backend
# varies slightly regardless), and seed is explicitly best-effort. It is
# the closest equivalent this model offers, and run-to-run drift should
# still be judged from real re-runs, not assumed away by the parameter.
#
# reasoning_effort is set explicitly rather than left to the API default,
# so a future change to that default can't silently move this project's
# cost or quality. "medium" matches the current default; "low" cuts
# reasoning tokens (and cost) if runs get expensive.
#
# gpt-5-mini rather than full gpt-5: output tokens are where an agent run's
# cost concentrates (a measured gpt-5 run spent 240,574 output tokens
# against 1.59M input, and output is the far pricier side per token -
# reasoning tokens are billed as output), so the cheaper tier is the first
# dial to turn. Full gpt-5 got this project's hardest judgment call - which
# PSS-10 items are reverse-coded - exactly right, 4/4, where gpt-4o-mini
# got 0/4; whether mini holds that accuracy is the thing to check on the
# next real run, since that judgment is the whole reason for moving off
# gpt-4o-mini in the first place. If it regresses, go back to "gpt-5" here
# rather than accepting a wrong alpha.
#
# Which provider actually serves that model is LLM_SOURCE's decision, not
# this file's: "openai" (the default) talks to api.openai.com, "azure"
# talks to an Azure OpenAI deployment. Both go through langchain_openai,
# so everything downstream - tools, middleware, run() - is unchanged; only
# the credentials and the endpoint differ. Azure names a *deployment*
# rather than a model, and the deployment name is chosen by whoever
# created it, so it can't be inferred here - it has to be given.
LLM_SOURCE = (os.getenv("LLM_SOURCE") or "openai").strip().lower()

# Named here rather than inline so switching model is an .env change, the
# same as switching provider.
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5-mini"

# Shared by both providers: see the reasoning above for why temperature is
# absent and seed/reasoning_effort are set explicitly.
# max_retries covers the one failure the tool-level @_with_retry cannot:
# a 429 from the model itself. That decorator wraps tools, and its
# retryable set is E2B's exceptions - nothing in this project ever retried
# an LLM call. A real run died on Azure's per-deployment TPM limit
# ("rate_limit_exceeded", eastus2) at turn 18 of ~20, having pushed 392,713
# tokens in 292s (~80,700 tokens/min); every token spent was lost because a
# transient, explicitly retryable error propagated as a crash. The SDK
# retries 429s with exponential backoff and honours Retry-After, so the
# fix is to give it enough attempts to outlast a quota window rather than
# the default 2.
#
# request_timeout stops the opposite failure - a call that hangs forever
# holding the run open - from being unbounded.
_MODEL_KWARGS = {
    "seed": 42,
    "reasoning_effort": "medium",
    "max_retries": 8,
    "request_timeout": 300,
}


def _require_env(*names: str) -> list[str]:
    """Return the values of `names`, or raise naming every one that is
    missing - not just the first. A half-configured Azure block is the
    normal failure here (endpoint set, deployment forgotten), and finding
    out one variable per re-run is needless."""
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        raise ValueError(
            f"LLM_SOURCE={LLM_SOURCE!r} needs these environment variables, "
            f"which are unset or empty: {', '.join(missing)}. Add them to "
            "excel-analysis-agent-backend/.env"
        )
    return [os.environ[n] for n in names]


def _build_model():
    """The chat model this run talks to, per LLM_SOURCE."""
    if LLM_SOURCE == "openai":
        _require_env("OPENAI_API_KEY")
        return ChatOpenAI(model=OPENAI_MODEL, **_MODEL_KWARGS)

    if LLM_SOURCE == "azure":
        endpoint, deployment, api_version, api_key = _require_env(
            "AZURE_API_BASE", "AZURE_DEPLOYMENT", "AZURE_API_VERSION", "AZURE_API_KEY"
        )
        return AzureChatOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_version=api_version,
            api_key=api_key,
            **_MODEL_KWARGS,
        )

    raise ValueError(
        f"LLM_SOURCE={LLM_SOURCE!r} is not one of 'openai', 'azure'. "
        "Set it in excel-analysis-agent-backend/.env"
    )


model = _build_model()

# deepagents ships a default tool suite of its own (ls/read_file/write_file/
# edit_file/delete/glob/grep for a virtual filesystem, execute for shell
# commands, task for subagents) that this project mostly doesn't use - we
# already have our own file-reading (read_excel_tool) and code-running
# (run_code_tool, sandboxed) tools. We exclude all of them except
# `read_file`: the summarization middleware below offloads messages it
# evicts from context to a virtual file *before* summarizing them, and its
# summary points the agent back at that file - if `read_file` were also
# excluded, the agent would have no way to recover the offloaded detail
# when it needed it. There's no `excluded_tools=` kwarg on create_deep_agent
# itself; exclusion is configured by registering a "harness profile" keyed to
# the model's provider ("openai" here, inferred from the ChatOpenAI instance)
# before building the agent. A middleware then strips these names from the
# tool list sent to the model on every call, so they don't cost schema tokens
# even though they're still technically registered. Note this registration
# is process-wide and keyed by provider, not by this specific agent
# instance - a second OpenAI-backed create_deep_agent() built elsewhere in
# the same process would silently inherit this exact exclusion set too.
#
# excluded_middleware drops create_deep_agent's own default summarization
# middleware (see docs/progress.md section 7): that default picks its
# trigger from the model's profile (`compute_summarization_defaults` in
# deepagents/middleware/summarization.py), 85% of the model's real
# max_input_tokens - that was ~109k on gpt-4o-mini (128,000 window) and is
# far higher on gpt-5, whose window is bigger again, so this default is
# even less reachable now than it was. Every run measured so far, even the
# largest 61-column file, has topped out around 74.4k tokens for its
# single biggest call - so that default never actually fires. We register
# our own SummarizationMiddleware below instead (summarization_middleware),
# with a trigger far below observed per-call sizes, so compaction can
# actually engage.
#
# Registered against the provider deepagents actually infers from `model`,
# NOT the literal "openai": AzureChatOpenAI reports its provider as
# "azure" (confirmed by calling deepagents._models.get_model_provider on
# both classes), so hardcoding "openai" here would silently register a
# profile that never matches once LLM_SOURCE=azure. The failure would be
# quiet and nasty in both directions - the eight excluded tools would come
# back, and, worse, the default SummarizationMiddleware would no longer be
# excluded, leaving it active *alongside* our own low-trigger replacement.
register_harness_profile(
    get_model_provider(model) or "openai",
    HarnessProfile(
        excluded_tools=frozenset(
            {
                "ls",
                "write_file",
                "edit_file",
                "delete",
                "glob",
                "grep",
                "execute",
                "task",
            }
        ),
        excluded_middleware=frozenset({"SummarizationMiddleware"}),
    ),
)

# Shared by both create_deep_agent (below) and our custom summarization
# middleware - they must be the same object, since the middleware offloads
# evicted messages to this exact backend before summarizing them.
# StateBackend keeps that offloaded file in graph state (in-memory), never
# written to real disk - what this project needs, since the source
# spreadsheet and conversation content must never touch the host
# filesystem. Swapping this for a disk-backed backend (e.g.
# FilesystemBackend) would silently start writing conversation history to
# disk.
backend = StateBackend()

# Shared by both create_deep_agent (below) and the token counter passed to
# the summarization middleware.
tools = [
    read_excel_tool,
    profile_tool,
    study_plan_tool,
    run_code_tool,
    recommend_test_tool,
    classify_columns_tool,
    infer_scale_tool,
    infer_scales_tool,
    group_items_tool,
    score_items_tool,
    recall_memory_tool,
    set_preference_tool,
]

# The default token_counter (count_tokens_approximately with no tools=)
# only estimates the conversation text - it doesn't count the token cost
# of the tool schemas resent on every single call. With 10 tools, that's a
# real chunk of every real request that the trigger below would otherwise
# never see, which is exactly why the first attempt at this fix (trigger=
# 8000, default counter) still logged 0 summarization events on a run
# whose real per-call input tokens (OpenAI's own count, from
# usage_metadata) had already passed 8000. Passing tools= here makes the
# estimate track what the model actually gets billed for.
token_counter = partial(count_tokens_approximately, tools=tools)

# A plain SummarizationMiddleware(...) instance would report the same
# .name ("SummarizationMiddleware") as create_deep_agent's own default
# instance - deepagents/middleware/summarization.py hardcodes that name for
# any instance of the exact base class. That means excluded_middleware=
# {"SummarizationMiddleware"} above would silently drop BOTH the default
# AND this one (confirmed by an actual run: 0 summarization events fired
# even after conversation size clearly passed the trigger below). A
# subclass reports its own class name instead, so excluding the default by
# its name leaves this one alone - deepagents' own docstring notes
# subclasses are meant to be used this way for exactly this reason.
class _LowTriggerSummarization(SummarizationMiddleware):
    """Same as SummarizationMiddleware, just under a different .name so
    excluding the built-in default doesn't also exclude this one."""


# trigger is a fixed token count, not create_deep_agent's default fraction-
# of-context-window, so it stays put when the model changes.
#
# It was 8,000 while the model was gpt-4o-mini (128k window), picked so
# compaction would engage at all rather than sit unreachable at the ~109k
# default. That proved the mechanism works, and then became the single
# biggest source of wrong answers: 8k is ~3% of gpt-5's input window, so
# it fired 6 times in the 13-step run in results/test-2 (evicting the
# column values the agent then guessed at - see _data_dictionary) and sent
# a gpt-5 run into an 83-turn restart loop, re-calling profile_tool /
# recall_memory_tool / classify_columns_tool from scratch each time it
# forgot it had already run them, burning 1.83M tokens without reaching an
# answer.
#
# 100,000 keeps compaction reachable (it is still well under gpt-5's
# input window, so a genuinely long run compacts rather than erroring)
# while leaving normal runs - the largest measured was 74.4k for its
# biggest single call - to complete without ever being compacted at all.
# keep=("messages", 6) matches the same fallback deepagents itself uses for
# models without profile info (compute_summarization_defaults' non-profile
# branch) - a reasonable, not invented, number of recent messages to always
# leave intact.
summarization_middleware = _LowTriggerSummarization(
    model=model,
    backend=backend,
    trigger=("tokens", 100_000),
    keep=("messages", 6),
    token_counter=token_counter,
)


def _run_gate_phase(file_path: str, question: str, assume_and_state: bool, brief: dict | None = None) -> dict:
    """Run the restricted-tool gate phase: load the file, decide whether
    the request is ambiguous, and capture the resulting decision straight
    off the submit_plan_tool call's arguments - not by parsing prose.

    Prints and accounts for this phase's trace/tokens/turns the same way
    run()'s own phase-2 loop does, and returns them (trace_lines,
    total_tokens, step_count) alongside the decision so run() can fold
    them into whole-run totals instead of only ever reporting phase 2's
    numbers - without this, gate-phase LLM turns/tokens never showed up
    anywhere, gate-phase messages never appeared in trace.log, and a run
    that ended in needs_clarification (spending real tokens) produced no
    trace at all.
    """
    handle_id = Path(file_path).stem
    gate_graph = create_deep_agent(
        model=model,
        tools=[read_excel_tool, profile_tool, submit_plan_tool],
        system_prompt=GATE_SYSTEM_PROMPT,
        backend=backend,
    )
    brief_text = _format_brief(brief)
    brief_block = (
        "\n\nANALYSIS BRIEF - supplied by the user, already checked against "
        "this file's real columns. Anything it states is settled: do not ask a "
        "clarifying question about it, and do not substitute your own choice "
        "for it. Anything it leaves out is still yours to decide.\n"
        + brief_text
        if brief_text
        else ""
    )
    gate_message = (
        f"Analyze the file at this path: {file_path}\n\n"
        f"Question: {question}\n\n"
        f"assume_and_state: {assume_and_state}"
        f"{brief_block}"
    )

    decision = None
    trace_lines: list[str] = []
    total_tokens = {"input": 0, "output": 0, "total": 0}
    step_count = 0
    n_seen = 0
    try:
        for step in gate_graph.stream(
            {"messages": [{"role": "user", "content": gate_message}]},
            stream_mode="values",
            config={"recursion_limit": MAX_GATE_STEPS},
        ):
            # Same "only print/account for messages new since the last step"
            # logic as phase 2's loop below - "values" mode re-yields the full
            # accumulated message list every step.
            messages = step["messages"]
            new_messages = messages[n_seen:]
            n_seen = len(messages)

            for message in new_messages:
                message.pretty_print()
                trace_lines.append(message.pretty_repr())

                usage = getattr(message, "usage_metadata", None)
                if message.type == "ai" and usage:
                    step_count += 1
                    total_tokens["input"] += usage.get("input_tokens", 0)
                    total_tokens["output"] += usage.get("output_tokens", 0)
                    total_tokens["total"] += usage.get("total_tokens", 0)
                    tokens_line = (
                        f"[tokens this call: {usage.get('input_tokens', 0)} in / "
                        f"{usage.get('output_tokens', 0)} out -- "
                        f"running total: {total_tokens['total']}]"
                    )
                    print(tokens_line)
                    trace_lines.append(tokens_line)

                if message.type == "ai" and message.tool_calls:
                    for call in message.tool_calls:
                        if call["name"] == "submit_plan_tool":
                            decision = call["args"]

            if decision is not None:
                break
    except GraphRecursionError:
        # decision stays None - _validate_plan_decision below already
        # defaults None to a clarifying question (fail toward asking, not
        # crashing or guessing), the same handling as a missing/malformed
        # submit_plan_tool call.
        note = f"[gate phase hit its {MAX_GATE_STEPS}-step limit without deciding]"
        print(note)
        trace_lines.append(note)

    return {
        "decision": _validate_plan_decision(decision, handle_id),
        "trace_lines": trace_lines,
        "total_tokens": total_tokens,
        "step_count": step_count,
    }


def run(
    file_path: str,
    question: str,
    assume_and_state: bool = False,
    brief: dict | None = None,
    study_plan_path: str | None = None,
) -> dict:
    """Ask the agent to analyze `file_path` and answer `question`.

    `brief` is an optional analysis brief - any of outcome / predictors /
    analysis_plan / notes, each on its own optional. It narrows what the
    agent would otherwise decide for itself and is never required: passing
    None (the default) leaves the fully autonomous behaviour untouched.
    Columns it names are validated against the file before the first LLM
    call, so a typo costs nothing instead of producing a confident analysis
    of the wrong column.

    Prints each step (tool calls, tool results, final answer) as it
    happens, so you can watch the agent's reasoning trace live, and writes
    the report/trace/results to outputs/<handle_id>/<timestamp>/ once done
    (FR-8) so they survive after the process exits - including on the
    needs_clarification path, so a run that spent real tokens but never
    reached an answer still leaves a record. Also tracks step count and
    per-tool latency (NFR-5) alongside the existing token counting, across
    BOTH the gate phase and phase 2, not just phase 2.
    Returns a dict: {'status': 'needs_clarification', 'question': str},
    {'status': 'done', 'answer': str}, or - only if phase 2 hits
    MAX_PHASE2_STEPS without producing a final answer (FR-7.3's guardrail) -
    {'status': 'step_limit_exceeded', 'answer': str} carrying an explanatory
    message rather than a real finding. A run that crosses MAX_RUN_TOKENS
    returns {'status': 'token_budget_exceeded', 'answer': str} the same way -
    also an explanation, never a finding.
    """
    handle_id = Path(file_path).stem

    # Cleared here (not just at module load) so calling run() more than
    # once in the same process doesn't mix this run's tool latencies with
    # a previous one's. Cleared before the gate phase runs so its tool
    # calls count toward this run's totals too.
    TOOL_CALLS.clear()

    # Before the gate phase, and so before the first paid call: a brief that
    # names a column this file does not have is a mistake worth catching for
    # free. Only the header is read - the gate phase loads the file properly
    # a moment later.
    if study_plan_path:
        STUDY_PLAN["plan"] = study_plan_module.load_study_plan(study_plan_path)

    if brief:
        validate_brief(brief, pd.read_csv(file_path, nrows=0).columns.tolist()
                       if Path(file_path).suffix.lower() == ".csv"
                       else pd.read_excel(file_path, nrows=0).columns.tolist())

    final_answer = ""
    # Both guardrail flags live out here, not inside the phase-2 block:
    # the reporting tail below reads them either way, including on the
    # path where phase 2 is skipped for being over budget already.
    budget_hit = False
    step_limit_hit = False
    total_tokens = {"input": 0, "output": 0, "total": 0}
    step_count = 0
    summarization_events = 0
    trace_lines: list[str] = []

    # One try/finally spanning BOTH phases, not just phase 2's loop.
    # _run_gate_phase (called first, below) always calls read_excel_tool,
    # which unconditionally creates a real E2B sandbox and installs
    # statsmodels into it - so the sandbox must close whichever way this
    # function exits: needs_clarification (the common path per this
    # project's own documented gate-phase limitation), a normal "done"
    # return, or an exception raised anywhere in either phase (e.g. the
    # gate phase's own graph erroring before ever calling
    # submit_plan_tool). Without wrapping the gate-phase call too, the
    # needs_clarification path returned before phase 2's try/finally even
    # started, leaking a real, billable sandbox until E2B's own 20-minute
    # timeout.
    try:
        gate_output = _run_gate_phase(file_path, question, assume_and_state, brief)
        gate_result = gate_output["decision"]
        trace_lines.extend(gate_output["trace_lines"])
        for key in total_tokens:
            total_tokens[key] += gate_output["total_tokens"][key]
        step_count += gate_output["step_count"]

        clarification_question = None
        if gate_result["status"] == "needs_clarification":
            clarification_question = gate_result["question"]
        elif gate_result["handle_id"] not in HANDLES:
            # status == "ready" but the model committed a plan without ever
            # calling read_excel_tool first - nothing currently prevents
            # that. There is no real sandbox_path to hand phase 2 in that
            # case, so fail toward asking rather than seeding phase 2 with
            # an empty sandbox_path alongside "don't call read_excel_tool
            # again," which would be actively wrong advice here.
            clarification_question = _FALLBACK_QUESTION

        if clarification_question is not None:
            print(f"\n=== Clarifying question ===\n{clarification_question}")
            output_dir = write_outputs(
                handle_id,
                f"[needs_clarification] {clarification_question}",
                trace_lines,
                total_tokens,
                step_count,
                list(TOOL_CALLS),
                summarization_events,
            )
            print(f"\n=== Outputs written to {output_dir} ===")
            return {"status": "needs_clarification", "question": clarification_question}

        # The gate phase's tokens are already folded into total_tokens
        # above. If that alone blew the ceiling, stop here rather than
        # opening phase 2 and spending at least one more (large) call to
        # discover the same thing.
        if total_tokens["total"] >= MAX_RUN_TOKENS:
            budget_hit = True
            note = (
                f"[token budget exceeded during the gate phase - "
                f"{total_tokens['total']:,} >= {MAX_RUN_TOKENS:,}, phase 2 not started]"
            )
            print(note)
            trace_lines.append(note)

        # Everything below is phase 2. Skipped entirely when the gate
        # phase alone already crossed MAX_RUN_TOKENS - building the
        # graph is cheap, but streaming it is not, and the whole point
        # of the ceiling is to not spend another large call to learn
        # what the token count already says.
        if not budget_hit:
            brief_text_for_prompt = _format_brief(brief)
            # Only that a plan EXISTS, its title, and how to read it - not
            # its contents. The plan is ~11,300 tokens; pinning even its
            # header would resend that on all ~15 calls for the sake of
            # steps that never look at it. What has to survive compaction is
            # the knowledge that the plan is there and which section answers
            # which question - the answers themselves are one tool call away.
            _plan = STUDY_PLAN.get("plan")
            study_plan_title = _plan.get("title") if _plan else None
            sandbox_path = SANDBOX_PATHS.get(gate_result["handle_id"], "")
            assumption_line = f"Assumption: {gate_result['assumption']}\n\n" if gate_result.get("assumption") else ""

            # The file handle, question, assumption, and task list all go into
            # phase 2's *system* prompt, not only the opening Human message - a
            # live run surfaced that the summarization middleware can offload the
            # opening message entirely (confirmed live: cutoff_index=3 after just
            # the second tool-call cycle, on a run that stopped after 2 of 6
            # task-list stages and never restated its assumption). A second live
            # run, after moving the task list/assumption/question here, showed the
            # SAME root cause hitting a second fact still left only in the opening
            # message: handle_id/sandbox_path also got summarized away
            # (cutoff_index=5 on the very first event), and with no tool call in
            # this phase-2 conversation to re-derive sandbox_path (read_excel_tool
            # is deliberately not called again here), the model guessed a
            # plausible-looking but wrong path ("/sandbox/...") for run_code_tool
            # instead - so it belongs in the same protected place. A system prompt
            # is resent in full on every model call and is never part of the
            # compactable message history (deepagents keeps it in
            # request.system_message, separate from request.messages, which is
            # all _determine_cutoff_index/_partition_messages ever touch) - baking
            # all of this in here is what keeps it in view no matter how early or
            # how often compaction fires. Building a fresh graph per run() call to
            # do this mirrors the pattern _run_gate_phase already uses for its own
            # per-call graph.
            phase2_system_prompt = (
                SYSTEM_PROMPT
                + "\n\nThis run's file handle, question, assumption (if any), and "
                "task list - restated here because the opening message carrying "
                "the same detail can be summarized away mid-run, and this cannot:\n"
                + f"handle_id: {gate_result['handle_id']}, sandbox_path: {sandbox_path}\n\n"
                + f"Question: {question}\n\n"
                + assumption_line
                + f"Task list:\n{_format_tasks(gate_result['tasks'])}\n\n"
                + (
                f"This dataset has a STUDY PLAN: {study_plan_title!r}. It is "
                "the design document stating what every column means, how "
                "each derived variable is built, which items are "
                "reverse-scored and on what anchor, and the analysis the "
                "study intended. It is NOT reproduced here - it is far too "
                "large - so read it a slice at a time with study_plan_tool, "
                "fetching only what the step in front of you needs:\n"
                "  section='overview'      research question, hypothesis, design, N\n"
                "  section='variables'     the outcome and predictor names\n"
                "  section='analysis'      the tests the study planned, and why\n"
                "  section='column_map'    question wording -> coded column name\n"
                "  section='item'          one item: anchor, options, reverse flag\n"
                "  section='items'         every item for an instrument/subscale\n"
                "  section='variable'      how one derived variable is built\n"
                "  section='reverse_coded' the reverse-scored item list\n"
                "Start by fetching 'overview', 'variables' and 'analysis' - "
                "you need those to plan at all. Prefer the plan over "
                "inferring from the data wherever it speaks: what it states "
                "is a fact about the study's design, and an item's "
                "reverse-coding and anchor in particular cannot be recovered "
                "reliably from observed values. If this file's headers are "
                "question wording rather than codes, fetch 'column_map' "
                "before classifying anything. A derived variable the plan "
                "names will not exist in the file yet - compute it as the "
                "plan specifies. If a variable's method is 'custom', the "
                "plan is deferring to a published algorithm it does not "
                "itself carry: say so plainly rather than inventing one. The "
                "planned tests are the study's intent, not a licence to skip "
                "checking - verify each test's assumptions against the real "
                "data, and say so if a planned test does not fit what the "
                "data shows.\n\n"
                if study_plan_title
                else ""
            )
            + (
                "The user's ANALYSIS BRIEF for this run. Pinned here for the "
                "same reason as everything else above: it cannot be "
                "summarized away. Whatever it states is settled - use those "
                "variables, follow that plan, and do not silently substitute "
                "your own choice. Whatever it leaves unstated is still yours "
                "to decide, exactly as if no brief had been given. If "
                "something in it turns out to be impossible against the real "
                "data (a named column has no variance, say), say so plainly "
                "before proceeding rather than quietly working around it.\n"
                + brief_text_for_prompt
                + "\n\n"
                if brief_text_for_prompt
                else ""
            )
            + "Every column's ACTUAL values in this file, counted from the "
                "data itself and pinned here for the same reason as everything "
                "above - it cannot be summarized away. This is ground truth "
                "about how this file is coded. Never assume or invent a coding "
                "scheme for a column listed here, and never filter on a value "
                "that does not appear in its list: doing so silently yields an "
                "empty group, and a test run on an empty group returns NaN "
                "rather than raising an error. If a test does come back NaN or "
                "empty, check the group sizes against this list before "
                "reporting anything about it - a NaN caused by your own filter "
                "matching no rows is not a finding about the data, and must "
                "never be reported as one.\n"
                + f"{_data_dictionary(HANDLES[gate_result['handle_id']])}\n\n"
                + "State any assumption above as the first line of your final "
                "answer. Work through every task-list stage in order (skipping "
                "only stages the list itself omits) before answering - if "
                "conversation summarization compacts earlier tool results out of "
                "view, the handle_id/sandbox_path, column values, and task list "
                "above still apply in full and are not themselves a signal that "
                "the work is done."
            )
            run_graph = create_deep_agent(
                model=model,
                tools=tools,
                system_prompt=phase2_system_prompt,
                backend=backend,
                middleware=[summarization_middleware],
            )
            # Deliberately NOT fully trimmed to just "Question: ...". A live
            # A/B test during this fix wave showed the difference matters: with
            # only "Question: ..." here (relying on phase2_system_prompt above
            # to carry the "don't call read_excel_tool again" instruction plus
            # handle_id/sandbox_path), the model called read_excel_tool again
            # anyway on its very first phase-2 turn, with a bare filename
            # (dropped the "data/" prefix), which doesn't resolve on the host
            # filesystem and crashed the run - reproduced twice in a row. Put
            # back exactly the same message with the explicit instruction and
            # concrete handle_id/sandbox_path restored: with that in place, a
            # third identical run called read_excel_tool exactly once (in phase
            # 1) and completed normally. So although phase2_system_prompt does
            # carry this same information in full (and survives compaction,
            # which is why it lives there too), a small model's compliance with
            # an instruction buried in a long static system prompt is not as
            # reliable as the same instruction restated in the message it's
            # actively responding to - keeping both is what's actually
            # reliable, not redundant in practice even though it's redundant on
            # paper.
            user_message = (
                f"This file is already loaded - do not call read_excel_tool again. "
                f"handle_id: {gate_result['handle_id']}, sandbox_path: {sandbox_path}\n\n"
                f"Question: {question}"
            )

            last_summarization_event = None
            n_seen = 0

            try:
                # Passed inline (not pulled into a variable first) so the type
                # checker matches this dict literal against the exact shape
                # create_deep_agent expects, instead of just inferring "some dict".
                for step in run_graph.stream(
                    {"messages": [{"role": "user", "content": user_message}]},
                    stream_mode="values",
                    config={"recursion_limit": MAX_PHASE2_STEPS},
                ):
                    # "values" mode yields the full accumulated message list after
                    # each graph step, not one message at a time. When the model
                    # makes several tool calls in one turn, langgraph runs them all
                    # in a single step and appends a ToolMessage per call - so
                    # printing only messages[-1] silently drops every result but
                    # the last one. Print/account for every message new since the
                    # last step instead.
                    messages = step["messages"]
                    new_messages = messages[n_seen:]
                    n_seen = len(messages)

                    # A real compaction event, not a text-pattern guess (past
                    # attempts to detect this by grepping the trace for words like
                    # "summary" produced false positives - see docs/progress.md
                    # section 7). This version of deepagents' summarization
                    # middleware works through wrap_model_call, not the older
                    # before_model hook - it never inserts a summary message into
                    # state["messages"] (confirmed by reading
                    # deepagents/middleware/summarization.py - only the *request*
                    # sent to the model is modified). The one place it does persist
                    # something to graph state is the private "_summarization_event"
                    # field (cutoff_index/summary_message/file_path) - compare it to
                    # the last one seen to count only genuinely new events, since it
                    # stays present in state across every later step once set.
                    current_event = step.get("_summarization_event")
                    if current_event is not None and current_event != last_summarization_event:
                        last_summarization_event = current_event
                        summarization_events += 1
                        event_line = (
                            f"[summarization fired - event #{summarization_events}, "
                            f"cutoff_index={current_event.get('cutoff_index')}, "
                            f"file_path={current_event.get('file_path')}]"
                        )
                        print(event_line)
                        trace_lines.append(event_line)

                    for message in new_messages:
                        message.pretty_print()
                        trace_lines.append(message.pretty_repr())

                        # Every AI message carries usage_metadata for that one LLM
                        # call (ChatOpenAI sets this); summing it across the trace
                        # gives the real token spend for the whole agent run, tool
                        # calls included.
                        usage = getattr(message, "usage_metadata", None)
                        if message.type == "ai" and usage:
                            step_count += 1
                            total_tokens["input"] += usage.get("input_tokens", 0)
                            total_tokens["output"] += usage.get("output_tokens", 0)
                            total_tokens["total"] += usage.get("total_tokens", 0)
                            tokens_line = (
                                f"[tokens this call: {usage.get('input_tokens', 0)} in / "
                                f"{usage.get('output_tokens', 0)} out -- "
                                f"running total: {total_tokens['total']}]"
                            )
                            print(tokens_line)
                            trace_lines.append(tokens_line)

                            # Checked here, immediately after this call's usage is
                            # folded in, so the run stops at the first call that
                            # crosses the line rather than after another full turn.
                            # Breaking out of the stream abandons the graph
                            # mid-run; that is safe because the sandbox is closed
                            # by the finally: below either way, and everything
                            # completed so far is already in trace_lines/store.
                            if total_tokens["total"] >= MAX_RUN_TOKENS:
                                budget_hit = True
                                note = (
                                    f"[token budget exceeded - {total_tokens['total']:,} >= "
                                    f"{MAX_RUN_TOKENS:,}, stopping before the next call]"
                                )
                                print(note)
                                trace_lines.append(note)
                                break

                        is_final_answer = message.type == "ai" and not message.tool_calls
                        if is_final_answer:
                            final_answer = message.content

                    if budget_hit:
                        break
            except GraphRecursionError:
                # FR-7.3: stop gracefully with partial results instead of
                # crashing run() - see MAX_PHASE2_STEPS's comment above for why
                # this exists. final_answer is deliberately overwritten (not
                # left as whatever partial text was accumulated, since a
                # not-final message was never meant to stand alone as the
                # answer) - trace_lines/results.json still carry everything
                # attempted before the cutoff for a human to inspect.
                step_limit_hit = True
                note = f"[phase 2 hit its {MAX_PHASE2_STEPS}-step limit before reaching a final answer]"
                print(note)
                trace_lines.append(note)
    finally:
        close_sandbox()

    if budget_hit:
        final_answer = (
            f"[token_budget_exceeded] Stopped at {total_tokens['total']:,} tokens, at or over "
            f"this run's {MAX_RUN_TOKENS:,}-token ceiling (MAX_RUN_TOKENS), before reaching a "
            "final answer. Nothing here was confirmed by a completed analysis - see trace.log "
            "for what was attempted. If the trace shows the same tools being called over and "
            "over, this is a retry loop and raising the ceiling will only cost more; if it "
            "shows steady progress on a genuinely large file, re-run with MAX_RUN_TOKENS set "
            "higher."
        )
    elif step_limit_hit:
        final_answer = (
            f"[step_limit_exceeded] Stopped after {MAX_PHASE2_STEPS} reasoning steps "
            "without reaching a final answer - almost certainly a retry loop rather "
            "than a genuinely long analysis (docs/progress.md documents real cases "
            "up to 297 repeated tool calls that the model never broke out of on its "
            "own). Nothing here was confirmed by a completed analysis; see trace.log "
            "for exactly what was attempted before the cutoff."
        )

    print(
        f"\n=== Token usage ===\n"
        f"input: {total_tokens['input']}, output: {total_tokens['output']}, "
        f"total: {total_tokens['total']}"
    )

    # Per-tool latency, aggregated from every call this run recorded via
    # agent_tools.py's @_timed decorator (NFR-5).
    tool_seconds: dict[str, float] = {}
    for call in TOOL_CALLS:
        tool_seconds[call["tool"]] = tool_seconds.get(call["tool"], 0.0) + call["seconds"]
    print(
        f"\n=== Steps: {step_count} LLM turns, {len(TOOL_CALLS)} tool calls ===\n"
        + "\n".join(f"  {name}: {seconds:.2f}s total" for name, seconds in tool_seconds.items())
    )
    print(f"\n=== Summarization events: {summarization_events} ===")

    output_dir = write_outputs(
        handle_id,
        final_answer,
        trace_lines,
        total_tokens,
        step_count,
        list(TOOL_CALLS),
        summarization_events,
    )
    print(f"\n=== Outputs written to {output_dir} ===")

    # FR-4.3: persist this run's conclusion against the file's schema so a
    # later run (even in a separate session) can see it via
    # recall_memory_tool. Uses the schema signature cached at load time
    # (store.SCHEMA_SIGS), NOT recomputed from HANDLES[handle_id] here -
    # score_items_tool mutates that same dataframe in place (adds
    # '{group}_score' columns), which would silently change the signature
    # and file this conclusion under a different key than the
    # classify/scale/group commits made earlier in this same run (caught
    # live: a real run's conclusion ended up unrecallable for exactly this
    # reason before this fix). handle_id is guaranteed present in
    # SCHEMA_SIGS here - reaching this line requires the gate phase's
    # "ready" + handle_id-in-HANDLES check to have passed above, and
    # read_excel_tool sets both HANDLES and SCHEMA_SIGS together.
    # Skip persisting a conclusion when the run never actually reached one -
    # a "stopped due to step limit" note is not a validated finding, and
    # recall_memory_tool's whole point is surfacing only validated judgment
    # calls to a later run (see its own docstring).
    # budget_hit joins step_limit_hit here for the same reason: a run that
    # stopped at a ceiling never reached a validated conclusion, and
    # recall_memory_tool exists to surface validated judgment calls to a
    # later run - persisting a "stopped early" note would poison that.
    if final_answer and not step_limit_hit and not budget_hit:
        long_term_memory.remember_conclusion(SCHEMA_SIGS[handle_id], question, final_answer[:2000])

    if budget_hit:
        return {"status": "token_budget_exceeded", "answer": final_answer}
    if step_limit_hit:
        return {"status": "step_limit_exceeded", "answer": final_answer}
    return {"status": "done", "answer": final_answer}
