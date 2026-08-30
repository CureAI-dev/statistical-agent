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

### 7. Subscale grouping and scoring
Satisfies: FR-9.4, FR-9.5, FR-9.6

- `tools.py: group_items()` computes a pairwise correlation matrix across
  a file's Likert items (using each item's already-committed scale from
  `infer_scale`) as a signal for which items might share a construct.
  Correlation can't say why items belong together or what to name the
  group, so it never invents a grouping - that judgment is left entirely
  to the caller, same division of labor as `infer_scale`'s reverse-coding.
- `group_items_tool` in `agent_tools.py`: call with no `groups` to see the
  correlation signal, call again with `groups={"name": [cols], ...}` (plus
  optional `rationale`) to commit it to `GROUPS`. Requires every named
  column to already have a committed scale.
- `tools.py: score_items()` applies each item's committed label->score map
  and reverse-coding, averages a group's items into one per-respondent
  score, and computes Cronbach's alpha for the group.
- `score_items_tool` writes the resulting `{group}_score` column(s) into
  the working dataframe and re-uploads it to the sandbox at its existing
  path, so `run_code_tool` sees the new column just by re-reading the CSV
  - no hand-built recreation needed. The tool result spells out the exact
  `score_column` name so the model can't typo or forget the `_score`
  suffix.
- Verified against three different files: the nurses file (single stress
  subscale, Cronbach's alpha 0.811 after a prompt fix - see below);
  the oncology file, which has *two* different Likert constructs mixed
  into one sheet (a 12-item patient-satisfaction scale and the same
  10-item stress scale) - the agent correctly split them into two
  separate subscales instead of forcing one group; and a file with no raw
  Likert items at all (pre-scored columns only), where `group_items_tool`
  was correctly never invoked.
- Fixed along the way: the model initially ignored `score_items_tool`'s
  own output and hand-rebuilt the score itself with a case-sensitive label
  map, producing silent zeros. Tightened `SYSTEM_PROMPT` to point at the
  tool's `score_column` field and forbid recomputing it by hand. Also
  caught the model retrying a raw-column-name-in-backticks statsmodels
  formula five times in a row (backticks don't reliably survive a `?` in
  the name either) instead of switching to renaming - prompt now rules out
  backtick-quoting explicitly and says to switch strategy after one
  failure, not repeat it.
- Known limitation, not a code bug: the agent's own reverse-coding
  judgment (from `infer_scale_tool`) is sometimes inconsistent across
  runs, occasionally producing a low or negative Cronbach's alpha. The
  tool correctly surfaces this (that's what alpha is for - see FR-9.6);
  `SYSTEM_PROMPT` now tells the agent to re-examine and fix reverse-coding
  when alpha comes back below ~0.5 before reporting, but a small model
  doesn't always follow through. This is a model-judgment quality issue,
  not something to paper over with a keyword heuristic (see the
  architecture decision on reverse-coding under `infer_scale` above).
- Stress-tested against a fourth, much larger file (184 rows, 61 columns,
  ~30 Likert items across three different scales stacked together -
  perceived stress, a 21-item anxiety/mood scale, and an 11-item sleep-
  disturbance scale). The agent correctly split all three into separate
  subscales (one run got a clean 0.913 Cronbach's alpha on the anxiety
  scale). This surfaced two real problems, one fixed and one still open:
  - **Fixed - fabricated result**: when the logistic regression's
    statsmodels formula kept failing, one run's final answer stated a
    specific chi-square p-value anyway, despite the same answer noting
    the statistic itself "was not computed due to issues with column
    access" - a fabricated number, not an honest failure report.
    `SYSTEM_PROMPT` now opens with a hard rule: never state a number that
    didn't come from an actual tool result this conversation; report a
    failed computation as failed instead. Re-verified against the same
    file afterward: the same class of failure (logistic regression
    couldn't get a working formula) was reported honestly with no
    invented numbers, and a chi-square that did succeed was double-checked
    against the raw tool output to confirm the reported figures were real.
  - **Still open, not yet fixed**: total token cost for this file was
    ~1-1.2M tokens (vs. ~50-150k for the smaller files) - roughly 10-20x
    more for about 3x more Likert items. Every LLM call resends the whole
    conversation so far with no compression, which is exactly what the
    not-yet-built memory system (FR-4-6) is for. Related: the agent
    reloads the CSV fresh with `pd.read_csv(sandbox_path)` in nearly every
    `run_code_tool` call instead of reusing the sandbox's persisted `df`,
    and re-cleans column names with a slightly different scheme each time
    (inconsistent case-folding, punctuation stripping) - so a name it
    "remembers" from an earlier cell often doesn't match the fresh
    reload, causing repeated `KeyError`/`PatsyError` failures (10-20 per
    run on this file). Not yet fixed; a likely candidate is telling the
    agent explicitly to reuse the persisted `df` and rename once, or to
    verify a rename actually took before building on it.

## Not built yet

- **FR-2**: parsing the analysis plan into an ordered, typed task list;
  asking a clarifying question by default when the plan is ambiguous.
- **FR-1.4, FR-1.5**: chunked reads for large files; detecting structural
  issues like merged cells or multi-row headers.
- **FR-4, FR-5, FR-6**: the memory tiers, context-budget tracking, and
  compression system.
- **FR-8**: written report/artifact output to a designated directory
  (currently the trace and answer just print to stdout).
- **NFR-2, NFR-5**: a persisted, structured audit trail and token/step/
  latency metrics (currently only the printed trace).
- **NFR-1, NFR-4, NFR-6**: deterministic re-runs, chunking for large
  files, and formal retry-with-backoff on tool failure.

## Architecture decisions made along the way

- `agent.py` was refactored into four files once it grew past ~450 lines
  mixing unrelated concerns: `prompts.py` (the system prompt), `store.py`
  (the committed-state dicts - `HANDLES`, `CLASSIFICATIONS`, `SCALES`,
  `GROUPS`, `SANDBOX_PATHS` - plus the `json_safe` helper), `agent_tools.py`
  (every `@tool`-wrapped function and the sandbox lifecycle), and `agent.py`
  itself, now just the model + `create_agent` wiring + `run()` (~110
  lines). No new abstractions or config layers - same plain functions and
  module-level dicts as before, just split by concern.
- `sandbox_tool.py: Runtime` now passes an explicit 20-minute `timeout` to
  `Sandbox.create()`. E2B's default is short and meant for one-off
  snippets; a real survey analysis sharing one sandbox across many tool
  calls (more Likert items = more calls) can outlive it, killing the
  sandbox mid-run with a `TimeoutException` - caught while testing against
  a 39-column file.
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
- Code: `excel-analysis-agent-backend/` - `agent.py` wires the model and
  tools together and runs the loop; `agent_tools.py` has the `@tool`
  wrappers; `prompts.py` has the system prompt; `store.py` has the
  committed-state dicts; `tools.py` holds the plain functions each tool
  wraps.
- Claude Code's session guidelines: `CLAUDE.md` at the repo root.
