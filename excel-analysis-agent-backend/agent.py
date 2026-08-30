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

from dotenv import load_dotenv
from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import StateBackend
from langchain_openai import ChatOpenAI

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
)
from prompts import SYSTEM_PROMPT

load_dotenv()

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
        )
    ),
)

# One call builds the whole "ask model -> run tool -> show result -> ask
# again" loop for us (this is the "ReAct" agent pattern), plus built-in
# summarization middleware that compresses old messages once the
# conversation gets long, instead of letting context grow unboundedly.
# `backend=StateBackend()` matters for more than the excluded filesystem
# tools above: the summarization middleware itself uses this exact backend
# object (deepagents/graph.py passes it straight into
# create_summarization_middleware(model, backend)) to offload messages it
# evicts from context to a conversation-history file *before* summarizing
# them, so they're not just discarded. StateBackend keeps that offloaded
# file in graph state (in-memory), never written to real disk - which is
# what this project needs, since the source spreadsheet and conversation
# content must never touch the host filesystem. Swapping this for a
# disk-backed backend (e.g. FilesystemBackend) would silently start writing
# conversation history to disk.
agent_graph = create_deep_agent(
    model=model,
    tools=[
        read_excel_tool,
        profile_tool,
        run_code_tool,
        recommend_test_tool,
        classify_columns_tool,
        infer_scale_tool,
        group_items_tool,
        score_items_tool,
    ],
    system_prompt=SYSTEM_PROMPT,
    backend=StateBackend(),
)


def run(file_path: str, question: str) -> str:
    """Ask the agent to analyze `file_path` and answer `question`.

    Prints each step (tool calls, tool results, final answer) as it
    happens, so you can watch the agent's reasoning trace live. Returns the
    final plain-English answer.
    """
    user_message = f"Analyze the file at this path: {file_path}\n\nQuestion: {question}"

    final_answer = ""
    total_tokens = {"input": 0, "output": 0, "total": 0}
    n_seen = 0
    try:
        # Passed inline (not pulled into a variable first) so the type
        # checker matches this dict literal against the exact shape
        # create_deep_agent expects, instead of just inferring "some dict".
        for step in agent_graph.stream(
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

            for message in new_messages:
                message.pretty_print()

                # Every AI message carries usage_metadata for that one LLM
                # call (ChatOpenAI sets this); summing it across the trace
                # gives the real token spend for the whole agent run, tool
                # calls included.
                usage = getattr(message, "usage_metadata", None)
                if message.type == "ai" and usage:
                    total_tokens["input"] += usage.get("input_tokens", 0)
                    total_tokens["output"] += usage.get("output_tokens", 0)
                    total_tokens["total"] += usage.get("total_tokens", 0)
                    print(
                        f"[tokens this call: {usage.get('input_tokens', 0)} in / "
                        f"{usage.get('output_tokens', 0)} out -- "
                        f"running total: {total_tokens['total']}]"
                    )

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

    return final_answer
