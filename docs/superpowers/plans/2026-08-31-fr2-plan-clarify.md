# FR-2 Plan Parsing + Clarifying Question Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `run()` into a restricted-tool gate phase that decides
ambiguous-or-not and produces a typed task list, followed by the existing
full-tool phase that does the real work only once the gate is past.

**Architecture:** A new small `create_deep_agent` graph (tools limited to
`read_excel_tool`, `profile_tool`, and a new `submit_plan_tool`) runs
first. It must end by calling `submit_plan_tool` with either a
clarifying question or a task list; `run()` reads that tool call's
arguments straight off the streamed message (no parsing of prose) and
branches. If it asked a question, `run()` returns immediately without
touching the existing full-tool agent at all. Otherwise the existing
agent runs exactly as it does today, seeded with the task list and told
not to reload the file.

**Tech Stack:** Python (`uv`-managed), `deepagents.create_deep_agent`,
`langchain_openai.ChatOpenAI`. No pytest suite exists anywhere in this
project (confirmed: no `test_*.py`/`*_test.py` files, no `[tool.pytest]`
in `pyproject.toml`) - all existing verification is either a direct
Python assertion check run via `uv run python -c "..."` for pure logic,
or a live run against a real sample file for agent behavior (see
`docs/progress.md`'s "Verified..." notes throughout). This plan follows
that same convention rather than introducing a test framework
unprompted.

**Spec:** `docs/superpowers/specs/2026-08-31-fr2-plan-clarify-design.md`

## Global Constraints

- Simplicity first - no new abstractions, classes, or config layers; plain
  functions and dicts, matching every existing file in this codebase.
- All data-processing code runs inside the E2B sandbox, never on the host
  (`Runtime.run_code()` only).
- The source spreadsheet is never modified - only the sandbox's working
  copy.
- Package manager is `uv` - do not add `pip`/`poetry` usage or a new
  dependency for this feature (everything needed already ships with
  `deepagents`/`langchain_core`).
- No new files unless a task explicitly says so - this feature modifies
  4 existing files only (`prompts.py`, `agent_tools.py`, `agent.py`,
  `main.py`).

**One implementation detail beyond what the spec pinned down:** the spec
says phase 1 "ends its turn with either a clarifying question... or a
task list" but didn't specify how `run()` detects which. This plan uses
a dedicated `submit_plan_tool` (Task 1) that phase 1 must call to end its
turn - the same suggest-then-commit-via-tool-call idiom every other
structured decision in this codebase already uses (`classify_columns_tool`,
`infer_scale_tool`, `group_items_tool`). `run()` reads the tool call's
arguments directly, rather than parsing free text out of a chat message.
This keeps the same architecture and task-list shape the spec approved;
it only fixes how the decision is captured in code.

---

### Task 1: `submit_plan_tool` - the gate phase's decision-commit tool

**Files:**
- Modify: `excel-analysis-agent-backend/agent_tools.py`

**Interfaces:**
- Produces: `submit_plan_tool(status: str, question: str | None = None, assumption: str | None = None, tasks: list[dict] | None = None) -> dict` - a `@tool`-decorated, `@_timed`-wrapped function. Later tasks (Task 4) read this tool's *call arguments* directly off the streamed `AIMessage.tool_calls`, not its return value - the return value only needs to be a valid, non-error dict so the graph doesn't treat the call as failed.

- [ ] **Step 1: Write a standalone check of the plain function**

Add this to the bottom of a throwaway scratch file (not committed) to confirm the function is callable and returns a dict before wiring it into anything else:

```python
# scratch check - not part of the codebase
import sys
sys.path.insert(0, "excel-analysis-agent-backend")
from agent_tools import submit_plan_tool

result = submit_plan_tool.func(
    status="ready",
    assumption=None,
    tasks=[{"step": "classify", "description": "classify columns", "status": "pending"}],
)
assert isinstance(result, dict), f"expected dict, got {type(result)}"
print("ok:", result)
```

- [ ] **Step 2: Run it to confirm it fails (function doesn't exist yet)**

Run: `uv run python scratch_check.py` (from `excel-analysis-agent-backend/`)
Expected: `ImportError: cannot import name 'submit_plan_tool'`

- [ ] **Step 3: Add `submit_plan_tool` to `agent_tools.py`**

Add near the bottom of the file, after `score_items_tool`:

```python
@tool
@_timed
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
```

- [ ] **Step 4: Re-run the scratch check to confirm it passes**

Run: `uv run python scratch_check.py`
Expected: `ok: {'received': True, 'status': 'ready'}`, then delete
`scratch_check.py` (throwaway, not committed).

- [ ] **Step 5: Commit**

```bash
git add excel-analysis-agent-backend/agent_tools.py
git commit -m "feat: add submit_plan_tool for gate-phase decision commit (FR-2)"
```

---

### Task 2: Gate phase prompt + phase-2 re-plan instruction

**Files:**
- Modify: `excel-analysis-agent-backend/prompts.py`

**Interfaces:**
- Produces: `GATE_SYSTEM_PROMPT: str` (new module-level constant). `SYSTEM_PROMPT` (existing constant) gains one appended paragraph - its name and existing content are unchanged, so every other file that already imports it needs no change.

- [ ] **Step 1: Write the check for both prompt strings**

```python
# scratch check - not part of the codebase
import sys
sys.path.insert(0, "excel-analysis-agent-backend")
from prompts import GATE_SYSTEM_PROMPT, SYSTEM_PROMPT

assert "submit_plan_tool" in GATE_SYSTEM_PROMPT
assert "needs_clarification" in GATE_SYSTEM_PROMPT
assert "assume_and_state" in GATE_SYSTEM_PROMPT
assert "task list" in SYSTEM_PROMPT.lower()
print("ok")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run python scratch_check.py`
Expected: `ImportError: cannot import name 'GATE_SYSTEM_PROMPT'`

- [ ] **Step 3: Add `GATE_SYSTEM_PROMPT` to `prompts.py`**

Add above `SYSTEM_PROMPT`:

```python
GATE_SYSTEM_PROMPT = (
    "You are the first step of a data-analysis agent for spreadsheets. "
    "Your only job: load the file, look at its real columns, and decide "
    "whether the question can be answered as asked. "
    "Call read_excel_tool, then profile_tool, to see the file's actual "
    "columns, types, and sample values. "
    "The user's message tells you whether assume_and_state is True or "
    "False. "
    "A request is ambiguous when: it names a column that isn't in the "
    "file's real columns; it asks to compare or predict something without "
    "saying what outcome or grouping to use, and more than one column "
    "could plausibly be it; or it's a generic 'analyze this file' with no "
    "stated goal at all. "
    "If the request is ambiguous and assume_and_state is False: call "
    "submit_plan_tool with status=\"needs_clarification\" and one "
    "specific question that names the actual columns involved - never a "
    "generic 'can you clarify?'. "
    "Otherwise (not ambiguous, or assume_and_state is True): if you're "
    "making an assumption instead of asking, state it in one sentence via "
    "the assumption argument. Then build a task list from these stages "
    "only, in order, skipping any that don't apply to this file or "
    "question: classify (tag columns as likert/categorical/open_ended/"
    "identifier/continuous), infer_scale (per-Likert-item point scale and "
    "reverse-coding), group (Likert items into subscales), score "
    "(compute subscale scores and Cronbach's alpha), test (run the "
    "statistical test the question calls for), report (write the final "
    "answer). Example: a file with no raw Likert items, already "
    "pre-scored, being asked for a chi-square test only needs "
    "[\"test\", \"report\"] - skip classify/infer_scale/group/score "
    "entirely. Call submit_plan_tool with status=\"ready\" and this task "
    "list. "
    "Call submit_plan_tool exactly once, as your last action. Never call "
    "any other tool after it."
)

```

- [ ] **Step 4: Append the re-plan paragraph to `SYSTEM_PROMPT`**

Find the last line of the existing `SYSTEM_PROMPT` tuple-of-strings (the
sentence ending "...Cronbach's alpha >= 0.7 is the conventional bar).")
and add one more string directly after it, still inside the same
parentheses (do not close and reopen the `(` ... `)`):

```python
    "You were given a task list at the start of this conversation, built "
    "from an initial read of the file, plus the handle_id and "
    "sandbox_path to use - do not call read_excel_tool again, the file is "
    "already loaded. If a later tool result contradicts one of the task "
    "list's steps or assumptions (e.g. an expected column is missing, a "
    "construct splits differently than planned), say so plainly in your "
    "next message before continuing - don't silently push through a plan "
    "that no longer fits what the data actually shows."
)
```

- [ ] **Step 5: Re-run the scratch check to confirm it passes**

Run: `uv run python scratch_check.py`
Expected: `ok`, then delete `scratch_check.py`.

- [ ] **Step 6: Commit**

```bash
git add excel-analysis-agent-backend/prompts.py
git commit -m "feat: add gate-phase prompt and re-plan instruction (FR-2)"
```

---

### Task 3: Pure decision-validation and task-list formatting helpers

**Files:**
- Modify: `excel-analysis-agent-backend/agent.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions, no I/O).
- Produces: `_validate_plan_decision(decision: dict | None, handle_id: str) -> dict` - returns `{"status": "needs_clarification", "question": str}` or `{"status": "ready", "handle_id": str, "assumption": str | None, "tasks": list[dict]}`. `_format_tasks(tasks: list[dict]) -> str`. Task 4 calls both.

- [ ] **Step 1: Write the failing checks**

```python
# scratch check - not part of the codebase
import sys
sys.path.insert(0, "excel-analysis-agent-backend")
from agent import _validate_plan_decision, _format_tasks

# no submit_plan_tool call happened at all
r = _validate_plan_decision(None, "nurses")
assert r["status"] == "needs_clarification"
assert r["question"]

# malformed: status="ready" but no tasks
r = _validate_plan_decision({"status": "ready"}, "nurses")
assert r["status"] == "needs_clarification"

# malformed: status="needs_clarification" but no question
r = _validate_plan_decision({"status": "needs_clarification"}, "nurses")
assert r["status"] == "needs_clarification"
assert r["question"]

# well-formed clarification
r = _validate_plan_decision({"status": "needs_clarification", "question": "Which outcome?"}, "nurses")
assert r == {"status": "needs_clarification", "question": "Which outcome?"}

# well-formed ready
tasks = [{"step": "test", "description": "run chi-square", "status": "pending"}]
r = _validate_plan_decision({"status": "ready", "tasks": tasks, "assumption": "Using Sex as the group."}, "nurses")
assert r == {"status": "ready", "handle_id": "nurses", "assumption": "Using Sex as the group.", "tasks": tasks}

# formatting
text = _format_tasks(tasks)
assert text == "- [pending] test: run chi-square"

print("ok")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run python scratch_check.py`
Expected: `ImportError: cannot import name '_validate_plan_decision'`

- [ ] **Step 3: Add both functions to `agent.py`**

Add near the top of `agent.py`, after the existing imports and before
the `model = ChatOpenAI(...)` line:

```python
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
```

- [ ] **Step 4: Re-run the scratch check to confirm it passes**

Run: `uv run python scratch_check.py`
Expected: `ok`, then delete `scratch_check.py`.

- [ ] **Step 5: Commit**

```bash
git add excel-analysis-agent-backend/agent.py
git commit -m "feat: add plan-decision validation and task-list formatting (FR-2)"
```

---

### Task 4: Gate phase runner + wiring into `run()`

**Files:**
- Modify: `excel-analysis-agent-backend/agent.py`

**Interfaces:**
- Consumes: `submit_plan_tool` (Task 1), `GATE_SYSTEM_PROMPT` (Task 2), `_validate_plan_decision`/`_format_tasks` (Task 3), existing `model`, `backend`, `read_excel_tool`, `profile_tool`, `close_sandbox`, `SANDBOX_PATHS` (needs importing from `store`).
- Produces: `_run_gate_phase(file_path: str, question: str, assume_and_state: bool) -> dict` (same return shape as `_validate_plan_decision`). `run(file_path: str, question: str, assume_and_state: bool = False) -> dict` - **return type changes from `str` to `dict`**: `{"status": "needs_clarification", "question": str}` or `{"status": "done", "answer": str}`. This is a breaking change to `run()`'s only caller (`main.py`, fixed in Task 5).

- [ ] **Step 1: Add the `SANDBOX_PATHS` import and `submit_plan_tool`/`GATE_SYSTEM_PROMPT` imports**

In `agent.py`, change:

```python
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
from store import TOOL_CALLS
```

to:

```python
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
```

- [ ] **Step 2: Add `_run_gate_phase` to `agent.py`**

Add after the `_format_tasks` function from Task 3, and after the
existing `agent_graph = create_deep_agent(...)` block (it needs `model`
and `backend`, already defined above it in the file):

```python
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
```

- [ ] **Step 3: Rewrite `run()`'s signature and opening block**

The current opening of `run()` (before this task) reads, in order:

```python
def run(file_path: str, question: str) -> str:
    """..."""
    handle_id = Path(file_path).stem
    user_message = f"Analyze the file at this path: {file_path}\n\nQuestion: {question}"

    # Cleared here (not just at module load) so calling run() more than
    # once in the same process doesn't mix this run's tool latencies with
    # a previous one's.
    TOOL_CALLS.clear()

    final_answer = ""
```

`user_message` must move to *after* the gate phase runs (it now depends
on `gate_result`), and `TOOL_CALLS.clear()` must run *before* the gate
phase (the gate phase's own tool calls are `_timed`-wrapped too and
should count toward this run's NFR-5 totals, not get cleared away
afterward). Replace that whole block with:

```python
def run(file_path: str, question: str, assume_and_state: bool = False) -> dict:
    """..."""
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
    user_message = (
        f"This file is already loaded - do not call read_excel_tool again. "
        f"handle_id: {gate_result['handle_id']}, sandbox_path: {sandbox_path}\n\n"
        f"Question: {question}\n\n"
        f"{assumption_line}"
        f"Task list:\n{_format_tasks(gate_result['tasks'])}"
    )

    final_answer = ""
```

(keep the existing docstring in place - only the code below it changes)

- [ ] **Step 5: Change the final return to the new dict shape**

Find the existing `return final_answer` at the end of `run()` and change
it to:

```python
    return {"status": "done", "answer": final_answer}
```

- [ ] **Step 6: Live verification - ambiguous question, no assumption**

This costs real OpenAI + E2B usage. Run from `excel-analysis-agent-backend/`:

```bash
uv run python -c "
from agent import run
result = run(
    'data/Perceived_Stress_and_Coping_Strategies_among_Nurses_in_Acute_simulated.csv',
    'Analyze this file.',
)
print(result)
"
```

Expected: `{'status': 'needs_clarification', 'question': '...'}` where the
question references real columns from the nurses file (not a generic
"please clarify"). Confirm via the printed trace that only
`read_excel_tool`, `profile_tool`, and `submit_plan_tool` were called -
no `classify_columns_tool`, `run_code_tool`, etc.

- [ ] **Step 7: Live verification - same question, `assume_and_state=True`**

```bash
uv run python -c "
from agent import run
result = run(
    'data/Perceived_Stress_and_Coping_Strategies_among_Nurses_in_Acute_simulated.csv',
    'Analyze this file.',
    assume_and_state=True,
)
print(result['status'])
print(result['answer'][:500])
"
```

Expected: `status == 'done'`, and the printed answer's start states an
assumption before getting into findings.

- [ ] **Step 8: Commit**

```bash
git add excel-analysis-agent-backend/agent.py
git commit -m "feat: wire gate phase into run(), change return to status dict (FR-2)"
```

---

### Task 5: Update `main.py` for the new return shape

**Files:**
- Modify: `excel-analysis-agent-backend/main.py`

**Interfaces:**
- Consumes: `run()`'s new `dict` return (Task 4).

- [ ] **Step 1: Update the `__main__` block**

Change:

```python
if __name__ == "__main__":
    answer = run(FILE_PATH, QUESTION)
    print("\n=== Final answer ===")
    print(answer)
```

to:

```python
if __name__ == "__main__":
    result = run(FILE_PATH, QUESTION)
    if result["status"] == "needs_clarification":
        print("\n=== Clarifying question ===")
        print(result["question"])
    else:
        print("\n=== Final answer ===")
        print(result["answer"])
```

- [ ] **Step 2: Live verification - full baseline run, unchanged behavior**

This is the existing smoke test's known-good question (already specific,
not ambiguous - names exact columns and exact outcome), so the gate phase
should pass it straight through. Costs real OpenAI + E2B usage.

Run: `uv run main.py` (from `excel-analysis-agent-backend/`)

Expected: same shape of final answer as the pre-FR-2 baseline recorded in
`docs/progress.md` §7 (classification, two subscales scored with
Cronbach's alpha, chi-square result, logistic regression result) - now
with a visible task list early in the trace, printed under
`=== Final answer ===` same as before (not
`=== Clarifying question ===`).

- [ ] **Step 3: Commit**

```bash
git add excel-analysis-agent-backend/main.py
git commit -m "feat: branch main.py smoke test on run()'s new status dict (FR-2)"
```

---

### Task 6: Update `docs/progress.md`

**Files:**
- Modify: `docs/progress.md`

- [ ] **Step 1: Add a new "Built" section for FR-2**, following the exact
  style of every existing section (cite FR numbers, name the functions
  touched, describe what was verified and how, note the one open item:
  phase 1 only catches question-level ambiguity from `read_excel_tool`/
  `profile_tool` output, not deeper structural ambiguity only visible
  after classification - FR-2.3's re-plan instruction is the intended
  catch for that, verify it separately if a real run surfaces one).

- [ ] **Step 2: Move the FR-2 line out of "Not built yet"** into the new
  "Built" section's heading reference.

- [ ] **Step 3: Commit**

```bash
git add docs/progress.md
git commit -m "docs: record FR-2 (plan parsing + clarifying question) as built"
```
