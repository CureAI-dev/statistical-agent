# Context Compression via deepagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the agent's per-run token cost from growing unbounded on
larger files by swapping in deepagents' built-in context-summarization
middleware, with no change to any existing tool.

**Architecture:** Replace `langchain.agents.create_agent` with
`deepagents.create_deep_agent` in `agent.py` only. The 8 existing
`@tool`-decorated functions in `agent_tools.py`, plus `tools.py`,
`store.py`, `prompts.py`, and the sandbox lifecycle, are untouched -
`create_deep_agent` takes the same LangChain tool interface. Compaction
of old conversation history into a summary happens automatically once a
call's context nears the model's limit; the code doing that work lives
inside the `deepagents` package, not in this repo.

**Tech Stack:** Python 3.14, `uv`, LangChain (`langchain`,
`langchain_openai`), new dependency `deepagents`, OpenAI `gpt-4o-mini`,
E2B sandbox (`e2b_code_interpreter`).

**Spec:** `docs/superpowers/specs/2026-08-30-context-compression-design.md`

## Global Constraints

- Only `excel-analysis-agent-backend/agent.py` changes code-wise; do not
  touch `agent_tools.py`, `tools.py`, `store.py`, `prompts.py`, or
  `sandbox_tool.py`.
- Use `StateBackend` (in-memory) for messages the middleware compacts
  away - no disk writes, no new persistence.
- Exclude `create_deep_agent`'s built-in filesystem tools
  (`ls`/`read_file`/`write_file`/`edit_file`/`delete`/`glob`/`grep`/
  `execute`) and its subagent `task` tool - this project doesn't use an
  agent-managed filesystem or subagents, and unused tool schemas cost
  tokens on every call.
- Leave the compression trigger threshold at the `deepagents` library
  default (~85% of the model's max input tokens) - do not hand-configure
  a different threshold.
- Package manager is `uv` - install the new dependency with
  `uv add deepagents`, never `pip install`.
- The user is cost-conscious: Task 3's real-world verification run costs
  real OpenAI + E2B spend (the pre-fix baseline for that same file was
  ~1-1.2M tokens). Do not re-run it more than once without a reason.

---

### Task 1: Add deepagents dependency and swap the agent construction in agent.py

**Files:**
- Modify: `excel-analysis-agent-backend/pyproject.toml` and
  `excel-analysis-agent-backend/uv.lock` (via `uv add`, not by hand)
- Modify: `excel-analysis-agent-backend/agent.py`

**Interfaces:**
- Consumes: `agent_tools.py`'s existing exports (`read_excel_tool`,
  `profile_tool`, `run_code_tool`, `recommend_test_tool`,
  `classify_columns_tool`, `infer_scale_tool`, `group_items_tool`,
  `score_items_tool`, `close_sandbox`) and `prompts.SYSTEM_PROMPT` -
  unchanged, same names, same shapes.
- Produces: `agent.py` still exposes a module-level `agent_graph` object
  with a `.stream(...)` method, and a `run(file_path: str, question:
  str) -> str` function - same public surface `main.py` already imports
  (`from agent import run`), so `main.py` needs no changes.

- [ ] **Step 1: Add the dependency**

Run from `excel-analysis-agent-backend/`:

```bash
uv add deepagents
```

Expected: command exits 0, `pyproject.toml` gains a `deepagents` entry,
`uv.lock` is updated. If it fails (dependency resolution conflict,
Python-version incompatibility), stop and report the exact error - do not
proceed to later steps.

- [ ] **Step 2: Confirm the import works**

```bash
uv run python -c "import deepagents; print(deepagents.__version__)"
```

Expected: prints a version string with no traceback.

- [ ] **Step 3: Introspect the real API before writing integration code**

The exact keyword arguments for excluding built-in tools and for
`create_deep_agent`'s model parameter are not pinned down in the design
doc (the docs describe the behavior but not a guaranteed exact
signature for this installed version) - confirm them for real instead of
guessing:

```bash
uv run python -c "
import inspect
import deepagents
print(inspect.signature(deepagents.create_deep_agent))
"
```

Read the printed signature. Find the parameter that lets you pass
`tools=[...]`, `model=...`, `system_prompt=...`, and `middleware=[...]`
or an equivalent (older docs called it `excluded_tools`; it may appear as
a top-level kwarg or nested in a `profile`/`AgentProfile`-style object -
whatever the actual signature shows takes precedence over any name used
elsewhere in this plan). Also run:

```bash
uv run python -c "
from deepagents.backends import StateBackend
print(StateBackend)
"
```

to confirm `StateBackend` imports from that path (if it errors, run
`uv run python -c "import deepagents; help(deepagents)"` and locate the
correct import path from the package's own docstring/listing).

- [ ] **Step 4: Read the current agent.py construction block**

Open `excel-analysis-agent-backend/agent.py` and find this block (it is
the entire body of the file below the imports and `load_dotenv()` call):

```python
model = ChatOpenAI(model="gpt-4o-mini")

agent_graph = create_agent(
    model,
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
)
```

- [ ] **Step 5: Replace the import**

In `excel-analysis-agent-backend/agent.py`, change:

```python
from langchain.agents import create_agent
```

to:

```python
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
```

(adjust the `StateBackend` import path if Step 3's introspection found a
different one).

- [ ] **Step 6: Replace the construction block**

Replace the block found in Step 4 with a call to `create_deep_agent`
using the same `model`, the same 8-tool list, and the same
`system_prompt`, plus the exclusion of built-in tools and the
`StateBackend`. Use the exact keyword names Step 3 confirmed. As a
starting template (adjust keyword names to match Step 3's real
signature):

```python
model = ChatOpenAI(model="gpt-4o-mini")

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
    excluded_tools=["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task"],
)
```

If Step 3 showed `model` must be a string (e.g. `"openai:gpt-4o-mini"`)
rather than a `ChatOpenAI` instance, keep the `ChatOpenAI` instance
construction removed and pass the string directly instead - note in your
task summary which form was actually required.

- [ ] **Step 7: Confirm the module imports without error**

```bash
uv run python -c "import agent; print(agent.agent_graph)"
```

Expected: prints a graph/object repr, no traceback. If it raises a
`TypeError` about an unexpected keyword argument, go back to Step 3 and
re-check the real signature - do not guess a second time.

- [ ] **Step 8: Commit**

```bash
cd "/home/abdul-rehman/Desktop/Autonomus Agent"
git add excel-analysis-agent-backend/pyproject.toml excel-analysis-agent-backend/uv.lock excel-analysis-agent-backend/agent.py
git commit -m "feat: swap create_agent for deepagents.create_deep_agent

Gets built-in SummarizationMiddleware for free to fix the measured
1-1.2M token blowup on larger files (docs/progress.md). All 8 existing
tools, tools.py, store.py, prompts.py, sandbox.py unchanged - same
LangChain tool interface. Built-in filesystem/subagent tools excluded
since this project doesn't use them and they'd cost schema tokens on
every call.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Cheap regression check against the small nurses file

**Files:**
- None modified - this is a verification-only task. May modify
  `excel-analysis-agent-backend/agent.py` again if Task 1's integration
  turns out to be broken in a way only visible at runtime (e.g. the
  trace-printing loop in `run()` doesn't match `create_deep_agent`'s
  stream shape).

**Interfaces:**
- Consumes: `agent.run(file_path, question)` from Task 1, and
  `excel-analysis-agent-backend/main.py`'s existing `FILE_PATH`/
  `QUESTION` (unchanged from prior sessions - the nurses CSV with the
  four-part classify/group-score/chi-square/logistic-regression
  question).

- [ ] **Step 1: Run the existing smoke test**

```bash
cd "/home/abdul-rehman/Desktop/Autonomus Agent/excel-analysis-agent-backend"
uv run main.py > /tmp/smoke_nurses_deepagents.log 2>&1
echo "EXIT_CODE=$?"
```

Expected: `EXIT_CODE=0`.

- [ ] **Step 2: Check for new failure modes in the trace**

```bash
grep -n "Traceback (most recent call last)" /tmp/smoke_nurses_deepagents.log | head -5
grep -c '"error": "' /tmp/smoke_nurses_deepagents.log
grep -n "=== Token usage ===" -A3 /tmp/smoke_nurses_deepagents.log
```

Expected: no bare Python traceback (a crash outside the agent's own
sandboxed `run_code_tool` calls - those already show up as JSON
`"error"` fields, which is normal and not a regression by itself). A
handful of `"error"` count similar to or lower than prior runs on this
file (recorded in `docs/progress.md`: past runs on this file ranged
roughly 0-4 tool-call errors) is fine; a much higher count, or a crash
that stops the run before printing `=== Token usage ===`, means Task 1's
integration has a real problem - go back to Task 1, not forward to Task
3.

- [ ] **Step 3: Confirm the full pipeline still completed**

```bash
grep -n "score_column\|Cronbach\|Chi-Square\|Logit Regression Results" /tmp/smoke_nurses_deepagents.log
```

Expected: all four are present at least once - classification/scoring
(`score_column`, `Cronbach`), the chi-square test, and the logistic
regression summary table. If any is missing, the agent gave up on a step
it used to complete - investigate before moving to Task 3.

- [ ] **Step 4: If Task 1's integration needed a fix, commit it**

Only if Step 2 or 3 required changing `agent.py` again:

```bash
cd "/home/abdul-rehman/Desktop/Autonomus Agent"
git add excel-analysis-agent-backend/agent.py
git commit -m "fix: correct create_deep_agent integration after smoke test

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

If no fix was needed, skip this step - there is nothing to commit for a
clean verification pass.

---

### Task 3: Real-world verification against the large stress-test file, and record the result

**Files:**
- Create (temporary, scratch): a small runner script pointing
  `agent.run(...)` at
  `data/Screen_media_multitasking_and_perceived_stress_in_clinical-y_simulated.csv`
  (mirror the pattern already used for the earlier oncology/urban-primary
  test runs this session - a standalone script that does
  `sys.path.insert(0, ".../excel-analysis-agent-backend")`, imports
  `run` from `agent`, and calls it with the same question used in the
  pre-fix baseline run).
- Modify: `docs/progress.md` (record the before/after token comparison).

**Interfaces:**
- Consumes: `agent.run(file_path: str, question: str) -> str` from Task 1.

- [ ] **Step 1: Write the runner script**

```python
import sys
sys.path.insert(0, "/home/abdul-rehman/Desktop/Autonomus Agent/excel-analysis-agent-backend")
from agent import run

FILE_PATH = "data/Screen_media_multitasking_and_perceived_stress_in_clinical-y_simulated.csv"
QUESTION = (
    "Classify every column as identifier, continuous, categorical, or Likert. "
    "This file mixes several different Likert constructs and some non-Likert "
    "numeric sleep-timing questions (bedtime, minutes to fall asleep, wake time, "
    "hours slept) in one sheet - don't treat those numeric sleep-timing questions "
    "as Likert items. Group the real Likert items into as many subscales as the "
    "wording actually supports (e.g. a perceived-stress scale, a "
    "depression/anxiety/stress-type scale, and a sleep-trouble scale are all "
    "plausibly present here) - don't force them into one group. For each subscale, "
    "compute its score and Cronbach's alpha and report whether the reliability is "
    "acceptable. Then run a chi-square test between 'Sex' and 'Have you been "
    "diagnosed with any long-term medical condition?'. Finally, run a logistic "
    "regression to predict 'Have you been admitted to hospital for this condition "
    "before?' using Age, Sex, and the computed stress subscale score. Present the "
    "findings clearly."
)

if __name__ == "__main__":
    answer = run(FILE_PATH, QUESTION)
    print("\n=== Final answer ===")
    print(answer)
```

Save it to a scratch path outside the repo (e.g. the session's scratchpad
directory), not inside `excel-analysis-agent-backend/` - it's a one-off
verification script, not part of the shipped code.

- [ ] **Step 2: Run it and capture full output**

```bash
cd "/home/abdul-rehman/Desktop/Autonomus Agent/excel-analysis-agent-backend"
uv run python /path/to/run_screenmedia_deepagents.py > /tmp/smoke_screenmedia_deepagents.log 2>&1
echo "EXIT_CODE=$?"
```

This is the expensive step - it costs real OpenAI + E2B spend, comparable
to the ~1-1.2M-token pre-fix baseline runs. Run it once; only re-run if
Step 3 or 4 below finds a bug that needs a fix-and-reverify cycle.

- [ ] **Step 3: Compare total token usage against the recorded baseline**

```bash
grep -n "=== Token usage ===" -A3 /tmp/smoke_screenmedia_deepagents.log
```

Compare the `total:` figure against the ~1-1.2M-token baseline recorded
in `docs/progress.md`'s "Subscale grouping and scoring" section (the
"Stress-tested against a fourth, much larger file" note). A materially
lower total (the fix should cap runaway per-call context, so the total
should drop, though by how much depends on how many times compaction
actually triggered) means the fix is working. If the total is about the
same or higher, the middleware likely never triggered - check Step 4
before concluding anything.

- [ ] **Step 4: Confirm per-call input tokens stay capped**

```bash
grep -oP '\[tokens this call: \d+ in' /tmp/smoke_screenmedia_deepagents.log | grep -oP '\d+' | sort -n | tail -5
```

In the pre-fix baseline, the largest single call reached ~57k input
tokens and kept climbing turn over turn. After the fix, the largest calls
should plateau near the ~85%-of-max-input-tokens trigger point (~109k for
gpt-4o-mini) rather than climbing without bound across the whole run -
though note `run()`'s own per-call token print reflects what
`usage_metadata` reports for the underlying model call, so also check
whether it stays roughly flat/repeating in the later half of the trace
rather than monotonically increasing, which is the actual signature of
compaction kicking in.

- [ ] **Step 5: Confirm no fabricated results**

```bash
grep -n "chi2_contingency\|smf.logit\|sm.Logit" /tmp/smoke_screenmedia_deepagents.log
```

For every numeric result in the final answer (chi-square statistic/
p-value, logistic regression coefficients), find the matching real tool
result earlier in the same log (a `"result":` line from `run_code_tool`
with matching numbers) - the existing `SYSTEM_PROMPT` rule against
fabricated numbers should already prevent this, but this stress test is
exactly the scenario that caught it fabricating a result once before, so
verify it directly rather than assuming the rule held.

- [ ] **Step 6: Record the result in docs/progress.md**

Add a short dated note under the existing "Stress-tested against a fourth,
much larger file" bullet in `docs/progress.md`'s section 7 (Subscale
grouping and scoring), stating: the token total from this run, whether it
dropped materially from the ~1-1.2M baseline, whether compaction visibly
engaged (per Step 4), and whether Step 5's fabrication check passed. If
the fix did not help (tokens stayed similarly high), say so plainly and
note that as a still-open problem rather than a resolved one.

- [ ] **Step 7: Commit the docs update**

```bash
cd "/home/abdul-rehman/Desktop/Autonomus Agent"
git add docs/progress.md
git commit -m "docs: record deepagents context-compression verification result

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```
