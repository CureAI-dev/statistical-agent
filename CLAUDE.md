# Claude Code Instructions

## What this project is
An autonomous agent that takes an Excel/CSV file + a plain-English analysis
request, and analyzes the data on its own (read → plan → run code → report).
Built for survey/questionnaire data specifically (Likert scales, scoring,
chi-square, logistic regression), but works on general spreadsheets too.

Full spec: `docs/requirements.md`. It's the source of truth for any
"should X work like Y" question.

## Current state (as of Aug 2026)
The core agent loop works and is verified against real sample files. Built
so far:
- `tools.py`: `read_excel()` and `profile()`, load a file into a pandas
  dataframe and summarize it (shape, dtypes, nulls, sample rows).
- `sandbox_tool.py`: `Runtime` class, wraps E2B's cloud sandbox so code
  runs in an isolated container, not on the host machine. Also captures
  the Jupyter-style auto-displayed value of a bare last expression, not
  just explicit `print()` output.
- `agent.py`: the actual agent. A LangChain `create_agent` ReAct loop with
  five tools (`read_excel_tool`, `profile_tool`, `run_code_tool`,
  `recommend_test_tool`, `classify_columns_tool`). One sandbox is shared
  per run so state persists across `run_code_tool` calls, which lets it
  transform data step by step (FR-9.7). `statsmodels` gets installed into
  the sandbox on startup (for the regression tests; `scipy` is already
  there). LLM is OpenAI (`gpt-4o-mini`) via `langchain_openai`.
- `recommend_test`: a plain, deterministic function in `tools.py` that
  implements the FR-9.9 test-selection table (t-test, ANOVA, chi-square,
  correlation, regression, ...). The lookup itself involves no LLM
  judgment. The agent runs a real normality check via `run_code_tool`
  first, then calls this to get the correct test name and function, then
  executes it. Verified end to end: it ran a Shapiro-Wilk check, fixed its
  own `NameError` from a wrong function name without help, then ran the
  right t-test and reported the assumptions and results.
- `classify_columns`: a function in `tools.py` (FR-9.1/FR-9.3) that
  suggests a type per column (likert, categorical, open_ended, identifier,
  continuous) from real signals: unique-value counts, top values, and
  whether the values match a built-in list of common Likert wordings
  (Never/Sometimes/Fairly often/Very often, Strongly disagree...Strongly
  agree, etc). The agent can override any suggestion after reading a
  column itself. Results land in `CLASSIFICATIONS` (agent.py, keyed by
  handle_id) so later steps (grouping items into subscales, scoring) can
  read them without re-deriving. Verified against two real files: it
  correctly separated identifier/continuous/categorical/likert columns
  and accepted the suggestions without needing an override on either.
- `main.py`: smoke test that runs the agent against one sample file.

Survey-mode pipeline (FR-9.1-9.8), built one step at a time: column
classification is done (above). Not built yet: infer_scale (per-item
scale + reverse-coding), group_items (subscale grouping), score_items
(scoring + Cronbach's alpha). Also not built: memory system, context
compression. Treat anything about those in requirements.md as a target,
not a description of existing code.

Open decision (see `docs/requirements.md` §10): whether the memory/
context-compression milestone should use LangChain's `deepagents` package
(built-in planning + summarization) instead of hand-rolling it. Not decided
yet; evaluate when we get there.

## Structure
```
Autonomus Agent/
├── CLAUDE.md                         this file
├── docs/requirements.md              full spec
└── excel-analysis-agent-backend/     the code (Python, uv-managed)
    ├── main.py                       smoke test
    ├── agent.py                      the agent: tools + create_agent loop
    ├── tools.py                      file reading, profiling, test picker
    ├── sandbox_tool.py               E2B sandbox wrapper
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
