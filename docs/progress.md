# Progress

What's actually built, checked against `requirements.md` (the spec). Every
item cites the FR/NFR section it satisfies, so this can be verified
against the spec directly rather than taken on faith.

## Built

### 1. File ingestion and profiling
Satisfies: FR-1.1, FR-1.2, FR-1.3

- `tools.py: read_excel()`, opens `.csv`, `.xlsx`, `.xls`, `.xlsm`.
- `tools.py: profile()`, returns dtypes, null counts, numeric summary, and
  sample rows, not the full table.
- Wrapped as `read_excel_tool` / `profile_tool` in `agent.py`.
- Verified against two real files (the nurses and oncology CSVs): correct
  row/column counts, correct null counts, correct sample data for both.

### 2. Core agentic loop
Satisfies: FR-7.1 (partial, see "Not built yet")

- `agent.py` uses LangChain's `create_agent` (the ReAct pattern): observe
  the state, decide which tool to call (or stop), act, observe the
  result, repeat.
- LLM is OpenAI `gpt-4o-mini` via `langchain_openai`.
- `create_agent`'s built-in `remaining_steps` already gives a step-cap
  guardrail (FR-7.3) with no extra code needed.

### 3. Sandboxed code execution
Satisfies: FR-3.3, NFR-3

- `sandbox_tool.py: Runtime` wraps E2B's cloud sandbox
  (`e2b_code_interpreter`); code never runs on this machine.
- `run_code_tool` in `agent.py` shares one sandbox per run, so variables
  the model defines stay alive across calls. This is what FR-9.7's
  step-by-step transformation needs.
- The source file is never mutated. `read_excel_tool` uploads a copy into
  the sandbox at a fixed path; the agent only ever touches that copy.
- The sandbox wrapper captures both explicit `print()` output and the
  Jupyter-style value of a bare last expression (`execution.text`), a gap
  caught during testing (the agent's code was producing results that were
  silently dropped before this fix).

### 4. Statistical test selection
Satisfies: FR-9.9, FR-9.10

- `tools.py: recommend_test()`, a deterministic lookup implementing the
  client's test-selection table: independent t-test, Mann-Whitney U,
  one-way ANOVA, Kruskal-Wallis, chi-square, Fisher's exact, Pearson,
  Spearman, binary logistic regression, linear regression.
- `recommend_test_tool` in `agent.py`, backed by scipy/statsmodels
  (`statsmodels` gets installed into the sandbox on startup, since only
  `scipy` ships with it by default).
- Verified end to end: the agent ran a real Shapiro-Wilk normality check,
  fixed its own `NameError` from a wrong function name without help,
  called `recommend_test_tool` with the real normality result (not the
  tool's default), ran the correct t-test, and reported the statistic,
  p-value, and a plain-language interpretation together.

### 5. Survey column classification
Satisfies: FR-9.1, FR-9.3

- `tools.py: classify_columns()` suggests a type per column, likert,
  categorical, open_ended, identifier, or continuous, from real signals:
  unique-value counts, top values, and whether the values match a
  built-in list of common Likert wordings.
- `classify_columns_tool` in `agent.py` lets the agent override any
  suggestion after reading a column itself; the result is stored in
  `CLASSIFICATIONS` (keyed by handle_id) for later steps to reuse without
  re-deriving it.
- Verified against two real files with different Likert wordings
  (frequency-style "Never...Very often" and agreement-style "Strongly
  disagree...Strongly agree"): correctly classified every column in both,
  no overrides needed.

### 6. Per-item Likert scale inference
Satisfies: FR-9.2

- `tools.py: infer_scale()` suggests one Likert item's point count and
  label->score map from its actual response values (numeric columns get
  an identity mapping at low confidence; text columns get matched against
  the same built-in Likert wordings `classify_columns` uses, at high
  confidence). It deliberately never guesses reverse-coding itself, since
  that depends on how an item's wording points relative to the construct
  it belongs to, not on the item's own values - that judgment is always
  left to the caller.
- `infer_scale_tool` in `agent.py` lets the agent override the scale or
  set `reverse_coded` after reading the item's wording against its
  subscale peers; the result is stored in `SCALES` (keyed by
  handle_id -> column) for later steps (scoring, reliability) to reuse.
- Verified end to end: the agent called `infer_scale_tool` once per
  Likert item (10 items), correctly matched the known "Never...Very
  often" wording each time, and made an explicit reverse-coded judgment
  per item instead of leaving the default.
- Fixed along the way: `run()`'s trace printer only showed the *last*
  message per `agent_graph.stream(..., stream_mode="values")` step, which
  silently dropped 9 of 10 tool results when the agent made parallel tool
  calls in one turn (the model still saw all 10; only the printed trace
  was misleading). Also tightened `SYSTEM_PROMPT` after catching the
  agent name a recommended test (e.g. via `recommend_test_tool`) without
  actually running it via `run_code_tool` and reporting a real
  statistic/p-value.

## Not built yet

- **FR-2**: parsing the analysis plan into an ordered, typed task list;
  asking a clarifying question by default when the plan is ambiguous.
- **FR-1.4, FR-1.5**: chunked reads for large files; detecting structural
  issues like merged cells or multi-row headers.
- **FR-4, FR-5, FR-6**: the memory tiers, context-budget tracking, and
  compression system.
- **FR-9.4**: `group_items`, subscale grouping.
- **FR-9.5, FR-9.6**: `score_items`, scoring plus Cronbach's alpha.
- **FR-8**: written report/artifact output to a designated directory
  (currently the trace and answer just print to stdout).
- **NFR-2, NFR-5**: a persisted, structured audit trail and token/step/
  latency metrics (currently only the printed trace).
- **NFR-1, NFR-4, NFR-6**: deterministic re-runs, chunking for large
  files, and formal retry-with-backoff on tool failure.

## Architecture decisions made along the way

- LLM: OpenAI (`gpt-4o-mini`), the user's choice over Anthropic.
- Agent framework: LangChain's `create_agent` (originally built on the
  now-deprecated `create_react_agent`; migrated after LangChain flagged
  the deprecation).
- Sandbox: E2B (`e2b_code_interpreter`), hand-rolled wrapper. No official
  LangChain+E2B integration exists, and LangChain's own sandbox package
  (Pyodide-based) is archived and deprecated.
- Open decision (`requirements.md` §10): whether the future memory/
  context-compression milestone should use LangChain's `deepagents`
  package instead of hand-rolling it. Not decided; revisit at that
  milestone.

## Where to look

- Full spec: `docs/requirements.md`.
- Code: `excel-analysis-agent-backend/` (`agent.py` is the entry point for
  the tool list and system prompt; `tools.py` holds the plain functions
  each tool wraps).
- Claude Code's session guidelines: `CLAUDE.md` at the repo root.
