# Claude Code Instructions

## What this project is
An autonomous agent that takes an Excel/CSV file + a plain-English analysis
request, and analyzes the data on its own (read → plan → run code → report).
Built for survey/questionnaire data specifically (Likert scales, scoring,
chi-square, logistic regression), but works on general spreadsheets too.

Full spec: `docs/requirements.md`. It's the source of truth for any
"should X work like Y" question.

For what's actually built vs. not, see `docs/progress.md`. It cites the
exact FR/NFR section each built piece satisfies, so check it before
believing any "is X done" claim, including the summary below.

## Current state (as of Aug 2026)
The core agent loop works and is verified against five real sample files
(two 50-row surveys, a 292-row oncology-satisfaction survey mixing two
different Likert constructs, a 50-row file with no raw Likert items at
all - already pre-scored, and a 184-row/61-column file with ~30 Likert
items across three different scales - the largest tried, and the one
that surfaced both the token-cost and retry-loop issues below). Built so
far, by file:

- `tools.py`: all the plain, LLM-judgment-free functions. `read_excel()`
  and `profile()` load a file into a pandas dataframe and summarize it
  (shape, dtypes, nulls, sample rows). `recommend_test()` implements the
  FR-9.9 test-selection table (t-test, ANOVA, chi-square, correlation,
  regression, ...) - a plain lookup, no LLM judgment. `classify_columns()`
  (FR-9.1/FR-9.3) suggests a type per column (likert/categorical/
  open_ended/identifier/continuous) from real signals. `infer_scale()`
  (FR-9.2) suggests one Likert item's point count and label->score map,
  but never guesses reverse-coding (that needs reading the item's wording
  against its construct, which only the agent can do). `group_items()`
  (FR-9.4) computes a correlation matrix across a file's Likert items as a
  grouping *signal* - it never invents the actual grouping or names a
  construct, same reasoning as reverse-coding. `score_items()` (FR-9.5/
  FR-9.6) applies each item's committed scale + reverse-coding, averages a
  group into one per-respondent score, and computes Cronbach's alpha.
- `sandbox_tool.py`: `Runtime` class, wraps E2B's cloud sandbox so code
  runs in an isolated container, not on the host machine. Captures the
  Jupyter-style auto-displayed value of a bare last expression, not just
  explicit `print()` output. Passes an explicit 20-minute timeout to
  `Sandbox.create()` - E2B's default is short and a longer survey analysis
  (more Likert items = more tool calls sharing one sandbox) can outlive
  it, which killed a run against the 39-column oncology file before this
  fix.
- `store.py`: the committed state every tool reads/writes, keyed by
  handle_id (`HANDLES`, `CLASSIFICATIONS`, `SCALES`, `GROUPS`,
  `SANDBOX_PATHS`), plus the `json_safe` helper that makes pandas/numpy
  values safe to send back to the model.
- `prompts.py`: the system prompt.
- `agent_tools.py`: the `@tool`-wrapped versions of every `tools.py`
  function, plus the sandbox lifecycle (`_get_sandbox`/`close_sandbox`).
  This is the layer that keeps the raw DataFrame out of the model's
  context (FR-3.1/FR-1.3) and makes sure data-processing code only runs in
  the sandbox. `classify_columns_tool`, `infer_scale_tool`, and
  `group_items_tool` all follow the same suggest-then-commit shape: call
  once to see a signal-based suggestion, call again with the agent's own
  judgment to override and commit it - later steps read the committed
  result instead of re-deriving it. `score_items_tool` is the one tool
  that mutates the working data: it writes the new `{group}_score`
  column(s) into the dataframe and re-uploads it to the sandbox at the
  same path, so `run_code_tool` picks it up just by re-reading the CSV.
- `agent.py`: ~170 lines - the model, the `deepagents.create_deep_agent`
  wiring (eight of our own tools, plus that package's built-in
  summarization middleware for compressing old messages once a
  conversation gets long), a harness-profile registration that excludes
  the deepagents built-in filesystem/subagent tools this project doesn't
  need (`ls`, `write_file`, `edit_file`, `delete`, `glob`, `grep`,
  `execute`, `task` - `read_file` stays available so the summarization
  middleware's own message-offload/recovery path still works), and
  `run()`. LLM is OpenAI (`gpt-4o-mini`) via `langchain_openai`. One
  sandbox is shared per run so state persists across `run_code_tool`
  calls (FR-9.7). `statsmodels` gets installed into the sandbox on
  startup (only `scipy` ships by default). `run()`'s trace printer walks
  every message new since the last stream step, not just the last one -
  `stream_mode="values"` batches parallel tool calls into one step with
  several new `ToolMessage`s, so printing only the last message silently
  dropped the rest of the trace (the model still saw them; only the
  printed log was incomplete).
- `report.py`: writes each run's `report.md`/`trace.log`/`results.json`
  to `outputs/<handle_id>/<timestamp>/` once `run()` finishes (FR-8) - no
  new dependency, no flag, happens automatically every run.
- `main.py`: smoke test that runs the agent against one sample file.

Verified end to end (see `docs/progress.md` for the full detail per
function): `recommend_test` picked the right test after a real normality
check and the agent self-corrected a `NameError` unaided; `classify_columns`
correctly typed every column across different Likert wordings;
`infer_scale` correctly matched known wordings and the agent made an
explicit reverse-coded call per item; `group_items`/`score_items`
correctly split two different Likert constructs (satisfaction vs. stress)
into two subscales on the oncology file instead of forcing one group, and
were correctly skipped on the file with no raw Likert items.

Known limitation, not a code bug: the agent's own reverse-coding judgment
is sometimes inconsistent across runs, occasionally producing a low or
negative Cronbach's alpha. The tool correctly surfaces this - that's
what alpha is for - but a small model (`gpt-4o-mini`) doesn't always loop
back and fix it before reporting, even though the prompt asks it to.

Survey-mode pipeline (FR-9.1-9.8) is now fully built (column
classification, scale inference, grouping, scoring). Not built: the
persistent, cross-session part of the memory system (FR-4.3 - schemas/
preferences/conclusions that survive between separate `uv run` calls).
Treat anything about that in requirements.md as a target, not a
description of existing code.

Decided (see `docs/requirements.md` §10 and `docs/progress.md`,
"Architecture decisions made along the way"): the in-session memory/
context-compression milestone (FR-5/FR-6) uses LangChain's `deepagents`
package (built-in summarization middleware) instead of hand-rolling it,
and this has shipped (`agent.py` now uses `create_deep_agent`). Whether
the compression itself actually helps is still unconfirmed - every run
tried so far, even the 61-column stress-test file, stays well under half
the ~109k token trigger needed to engage it, so the middleware has never
actually turned on.

**Real win from this same push, 2026-08-30**: a separate bug is fixed -
the agent used to call `infer_scale_tool` repeatedly (up to 297 times in
one run) against Likert items with unfamiliar wording instead of
supplying an explicit `label_to_score` on retry. `SYSTEM_PROMPT` now
forbids repeating that call unchanged and shows a worked example.
Result: the 61-column stress-test file completed its full pipeline
cleanly for the first time ever (classification, two subscales scored,
chi-square, logistic regression, all numbers verified real) at 732,755
tokens - down from 1,676,847 the prior attempt and from the original
~1-1.2M baseline. That drop is from the retry-loop fix, not from
compaction. See `docs/progress.md` §7 for the full detail.

## Structure
```
Autonomus Agent/
├── CLAUDE.md                         this file
├── docs/requirements.md              full spec
├── docs/progress.md                  what's built vs. not, cited against the spec
└── excel-analysis-agent-backend/     the code (Python, uv-managed)
    ├── main.py                       smoke test
    ├── agent.py                      model + create_deep_agent wiring + run()
    ├── agent_tools.py                @tool wrappers + sandbox lifecycle
    ├── prompts.py                    the system prompt
    ├── store.py                      committed-state dicts + json_safe
    ├── tools.py                      the plain, LLM-judgment-free functions
    ├── report.py                     writes per-run report/trace/results
    ├── sandbox_tool.py               E2B sandbox wrapper
    ├── outputs/                      generated per-run outputs (gitignored)
    ├── data/                         sample CSVs for testing
    └── .env                          API keys (never commit this)
```

## Rules for working on this repo
1. **Simplicity first.** This is a v1 built by someone learning as they go.
   Don't add abstractions, config layers, or "just in case" flexibility.
   If a plain function does the job, don't make it a class/framework.
2. **Never run arbitrary code on the host.** All data-processing code goes
   through `Runtime.run_code()` (the E2B sandbox), never local `exec`.
3. **The source spreadsheet is never modified.** Agent works on a copy in
   the sandbox.
4. **Explain things plainly.** The user knows what a "tool" is and not
   much more yet. Don't use jargon without a one-line explanation.
5. Package manager is `uv` (see `pyproject.toml`, `uv.lock`). Don't switch
   to pip/poetry.

## How to run the smoke test
```
cd excel-analysis-agent-backend
uv run main.py
```
Needs `E2B_API_KEY` and `OPENAI_API_KEY` set in `.env`.
