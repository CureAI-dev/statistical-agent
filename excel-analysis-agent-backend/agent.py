"""
The agent itself: an LLM that can look at a spreadsheet and answer
questions about it by calling tools, instead of us hardcoding the steps.

- `tools.py` has the plain functions that actually touch data.
- `agent_tools.py` wraps those functions as tools the model is allowed to
  call, keeping the raw DataFrame out of the model's context (see its own
  module docstring for the tool-by-tool breakdown) and running any code
  the model writes inside the E2B sandbox (`sandbox_tool.py`), never here.
- `store.py` holds the committed state each tool reads/writes (loaded
  files, column classifications, Likert scales, subscale groupings).
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

from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import StateBackend
from deepagents.middleware import SummarizationMiddleware
from langchain_core.messages.utils import count_tokens_approximately
from langchain_openai import ChatOpenAI

from report import write_outputs

from agent_tools import (
    classify_columns_tool,
    close_sandbox,
    group_items_tool,
    infer_scale_tool,
    profile_tool,
    read_excel_tool,
    recommend_test_tool,
    run_code_tool,
    score_items_tool,
    submit_plan_tool,
)
from prompts import GATE_SYSTEM_PROMPT, SYSTEM_PROMPT
from store import SANDBOX_PATHS, TOOL_CALLS

load_dotenv()

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
    if status == "ready" and decision.get("tasks"):
        return {
            "status": "ready",
            "handle_id": handle_id,
            "assumption": decision.get("assumption"),
            "tasks": decision["tasks"],
        }
    return {"status": "needs_clarification", "question": _FALLBACK_QUESTION}


def _format_tasks(tasks: list[dict]) -> str:
    """Render the committed task list as plain text for phase 2's opening
    message."""
    return "\n".join(
        f"- [{task.get('status', 'pending')}] {task.get('step')}: {task.get('description', '')}"
        for task in tasks
    )


# The model that decides which tool to call and when. gpt-4o-mini is cheap
# and more than capable of this kind of tool-picking + summarizing task.
model = ChatOpenAI(model="gpt-4o-mini")

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
# deepagents/middleware/summarization.py), 85% of gpt-4o-mini's real
# max_input_tokens (128,000, confirmed by reading `ChatOpenAI(model=
# "gpt-4o-mini").profile`) = ~109k. Every run measured so far, even the
# largest 61-column file, has topped out around 74.4k tokens for its
# single biggest call - so that default never actually fires. We register
# our own SummarizationMiddleware below instead (summarization_middleware),
# with a trigger far below observed per-call sizes, so compaction can
# actually engage.
register_harness_profile(
    "openai",
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
    run_code_tool,
    recommend_test_tool,
    classify_columns_tool,
    infer_scale_tool,
    group_items_tool,
    score_items_tool,
]

# The default token_counter (count_tokens_approximately with no tools=)
# only estimates the conversation text - it doesn't count the token cost
# of the tool schemas resent on every single call. With 8 tools, that's a
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
# of-context-window, chosen well below every per-call size actually
# observed on real runs (max seen so far: 74.4k) so it can engage instead
# of sitting unreachable. keep=("messages", 6) matches the same fallback
# deepagents itself uses for models without profile info
# (compute_summarization_defaults' non-profile branch) - a reasonable, not
# invented, number of recent messages to always leave intact.
summarization_middleware = _LowTriggerSummarization(
    model=model,
    backend=backend,
    trigger=("tokens", 8000),
    keep=("messages", 6),
    token_counter=token_counter,
)


def _run_gate_phase(file_path: str, question: str, assume_and_state: bool) -> dict:
    """Run the restricted-tool gate phase: load the file, decide whether
    the request is ambiguous, and capture the resulting decision straight
    off the submit_plan_tool call's arguments - not by parsing prose."""
    handle_id = Path(file_path).stem
    gate_graph = create_deep_agent(
        model=model,
        tools=[read_excel_tool, profile_tool, submit_plan_tool],
        system_prompt=GATE_SYSTEM_PROMPT,
        backend=backend,
    )
    gate_message = (
        f"Analyze the file at this path: {file_path}\n\n"
        f"Question: {question}\n\n"
        f"assume_and_state: {assume_and_state}"
    )

    decision = None
    for step in gate_graph.stream(
        {"messages": [{"role": "user", "content": gate_message}]},
        stream_mode="values",
    ):
        for message in step["messages"]:
            if message.type != "ai" or not message.tool_calls:
                continue
            for call in message.tool_calls:
                if call["name"] == "submit_plan_tool":
                    decision = call["args"]
        if decision is not None:
            break

    return _validate_plan_decision(decision, handle_id)


def run(file_path: str, question: str, assume_and_state: bool = False) -> dict:
    """Ask the agent to analyze `file_path` and answer `question`.

    Prints each step (tool calls, tool results, final answer) as it
    happens, so you can watch the agent's reasoning trace live, and writes
    the report/trace/results to outputs/<handle_id>/<timestamp>/ once done
    (FR-8) so they survive after the process exits. Also tracks step count
    and per-tool latency (NFR-5) alongside the existing token counting.
    Returns the final plain-English answer.
    """
    handle_id = Path(file_path).stem

    # Cleared here (not just at module load) so calling run() more than
    # once in the same process doesn't mix this run's tool latencies with
    # a previous one's. Cleared before the gate phase runs so its tool
    # calls count toward this run's totals too.
    TOOL_CALLS.clear()

    gate_result = _run_gate_phase(file_path, question, assume_and_state)
    if gate_result["status"] == "needs_clarification":
        return {"status": "needs_clarification", "question": gate_result["question"]}

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
        + "State any assumption above as the first line of your final "
        "answer. Work through every task-list stage in order (skipping "
        "only stages the list itself omits) before answering - if "
        "conversation summarization compacts earlier tool results out of "
        "view, the handle_id/sandbox_path and task list above still apply "
        "in full and are not themselves a signal that the work is done."
    )
    run_graph = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=phase2_system_prompt,
        backend=backend,
        middleware=[summarization_middleware],
    )
    user_message = (
        f"This file is already loaded - do not call read_excel_tool again. "
        f"handle_id: {gate_result['handle_id']}, sandbox_path: {sandbox_path}\n\n"
        f"Question: {question}"
    )

    final_answer = ""
    total_tokens = {"input": 0, "output": 0, "total": 0}
    step_count = 0
    summarization_events = 0
    last_summarization_event = None
    trace_lines: list[str] = []
    n_seen = 0
    try:
        # Passed inline (not pulled into a variable first) so the type
        # checker matches this dict literal against the exact shape
        # create_deep_agent expects, instead of just inferring "some dict".
        for step in run_graph.stream(
            {"messages": [{"role": "user", "content": user_message}]},
            stream_mode="values",
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

                is_final_answer = message.type == "ai" and not message.tool_calls
                if is_final_answer:
                    final_answer = message.content
    finally:
        close_sandbox()

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

    return {"status": "done", "answer": final_answer}
