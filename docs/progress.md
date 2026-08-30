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

- `agent.py` uses `deepagents.create_deep_agent` (originally LangChain's
  `create_agent`, swapped 2026-08-30 - see "Architecture decisions"
  below), the ReAct pattern: observe the state, decide which tool to
  call (or stop), act, observe the result, repeat, plus that package's
  built-in summarization middleware for compressing old messages once a
  conversation gets long.
- LLM is OpenAI `gpt-4o-mini` via `langchain_openai`.
- The underlying ReAct loop's built-in `remaining_steps` already gives a
  step-cap guardrail (FR-7.3) with no extra code needed.

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
    run on this file). Tried a prompt fix: told the agent to load+clean
    column names ONCE and reuse `df` across calls instead of
    reloading+re-cleaning every time. Tested on the small nurses file:
    still took 4 attempts to get a rename right (this time missing one
    needed column from the rename dict, not a cleaning-scheme mismatch),
    no real improvement over the pre-fix baseline. Conclusion: this isn't
    really a prompt-wording problem - it's `gpt-4o-mini` (chosen as the
    cheap/testing model) being unreliable at a task requiring it to
    remember and reuse an exact set of names consistently across many
    tool calls. A stronger model would likely need little to none of this
    prompting. Leaving the prompt addition in (it doesn't hurt) but not
    chasing this further with more prompt tuning - not worth the token
    spend for a model-capability gap.
  - **2026-08-30 re-verification after swapping `create_agent` for
    `deepagents.create_deep_agent`** (built-in summarization middleware,
    meant to fix the token-cost problem above): ran the same file/question
    once. Result is inconclusive-to-negative, not a confirmed fix.
    Total was **1,676,847 tokens - higher than the ~1-1.2M baseline**, not
    lower. Per-call input tokens climbed monotonically the whole run (3.1k
    -> 74.4k across 37 model turns) and never plateaued or reset, and no
    summary/compaction marker appears anywhere in the log - the largest
    single call (74.4k) never reached the ~109k trigger point (85% of
    gpt-4o-mini's context window), so **the summarization middleware never
    engaged at all this run**. That's the real reason the total didn't
    drop: the fix had no chance to act, not that it acted and failed.
    Separately, and likely the actual reason this run cost more than
    baseline: the agent got stuck in a retry loop unrelated to
    compression. It called `infer_scale_tool` 297 times and
    `group_items_tool` 12 times (only 1 `classify_columns_tool` call, and
    **zero** `score_items_tool` or `run_code_tool` calls the whole run) -
    it kept re-calling `infer_scale_tool` with the same arguments against
    the same ~21 DASS-21-style anxiety items, each time getting back
    "Unrecognized wording ... decide the label order yourself" (an
    uncommitted scale), never following up with an explicit
    `label_to_score` to actually commit one, so every `group_items_tool`
    call on that construct failed with the same "needs a committed scale
    first" error 12 times over. It never scored any subscale and never
    ran the chi-square or logistic regression, and gave up with a final
    answer asking the user to clarify wording instead. This is the same
    class of small-model judgment gap already noted above (reverse-coding
    consistency) rather than a new code bug, and not something introduced
    by the `deepagents` swap - so no code change was made here. Because
    the run never finished, comparing its total tokens to the baseline
    (which did finish the full task) isn't a clean apples-to-apples
    comparison either way. **Fabrication check passed**: `grep` for
    `chi2_contingency|smf.logit|sm.Logit` found zero matches outside the
    original question text, and the final answer stated no chi-square,
    regression, alpha, or subscale-score numbers at all - it honestly
    reported what it couldn't finish rather than inventing figures. But
    this is a weak pass: the check had nothing to catch, since no
    numeric claims were made this run. **Net conclusion: still open.**
    This run doesn't demonstrate the compaction fix helping, because
    the conversation never grew large enough in a single call to trigger
    it before an unrelated retry loop ended the run early. A real test of
    whether `create_deep_agent`'s summarization caps cost on this file
    would need a run that actually completes the full task (or a smaller
    context-window model, or a lower trigger threshold) to find out
    whether compaction engages once triggered - that's still unverified.
    **Sharper framing, on closer look:** "unverified" understates it.
    The library's default trigger (~109k tokens, 85% of `gpt-4o-mini`'s
    ~128k window) was already unreachable by this project's actual
    workload even before the retry-loop derailment - the *original*
    pre-fix baseline's largest single call only reached ~57k tokens
    (see the earlier baseline note above), and this re-verification
    run's max (74.4k) is still well under half the trigger. Every run
    observed so far, including on the largest file tried, stays well
    under half the threshold needed to ever engage the middleware. So
    the honest state isn't "we haven't tested it enough to know" - it's
    "as configured, this doesn't fit the actual shape of this project's
    per-call token sizes, independent of whether a run finishes
    cleanly." Lowering the trigger threshold and re-testing would need
    another expensive run and is a deliberate follow-up decision for
    later, not done as part of this note.
    Given the cost of this run, no immediate re-run is planned; revisit
    if/when the retry-loop behavior above gets addressed.
  - **2026-08-30, retry-loop fixed**: root cause was `infer_scale_tool`
    returning a low-confidence, uncommitted result for wording it didn't
    recognize (by design - see FR-9.2 above), and the agent calling it
    again unchanged instead of supplying an explicit `label_to_score` -
    297 identical wasted calls in the run above. `SYSTEM_PROMPT` now
    explicitly forbids repeating that call unchanged and gives a worked
    example of building `label_to_score` from the item's own listed
    values. Verified cheaply against a 6-column subset of the exact
    items that looped before (not the full expensive file): all 6 scored
    correctly on the first call, zero repeats, Cronbach's alpha 0.864,
    50k tokens total. Not yet re-verified on the full 61-column file
    (that's the expensive run this fix exists to make worth retrying) -
    doing so would also be the first real test of whether the
    `deepagents` compaction fix helps, now that the thing blocking a
    clean run is out of the way.
  - **2026-08-30, re-run after the retry-loop fix**: ran the same file
    again. First time this file has ever completed the full pipeline
    cleanly - classification, two subscales committed (Perceived Stress,
    alpha 0.429, poor; Anxiety, alpha 0.914, good), chi-square
    (χ²=1.91, p=0.167), and a logistic regression (via sklearn this time,
    accuracy 56.8%). All three numeric results independently checked
    against their raw tool outputs - real, not fabricated.
    `infer_scale_tool` called 52 times (vs. 297 before) - the retry loop
    is gone. Total cost **732,755 tokens**, down from 1,676,847 last
    attempt and from the original ~1-1.2M baseline - a real, large drop.
    But: the drop is entirely explained by the retry loop being gone
    (fewer wasted calls), not by compaction. Checked again: max per-call
    input was 42,350, still well under the ~109k trigger, and no genuine
    summarization/compaction marker appears in the log (the same false
    positives as before - "numeric_summary" JSON key, "Analysis Summary"
    heading). **The deepagents compaction fix still has not been shown to
    do anything on this project** - every run so far, on every file
    tried, stays well under half the threshold needed to engage it. One
    quality note (not a regression, likely just LLM variance): this run
    only formed 2 subscales instead of the 3 a much earlier, incomplete
    run had found (it never separated out the sleep-disturbance items
    this time) - grouping quality still isn't perfectly consistent
    run-to-run, a known limitation noted above under `infer_scale`.
  - **2026-08-30, compaction actually fixed and confirmed firing for
    real**: root cause turned out to be three separate bugs stacked on
    top of each other, not just "the threshold is too high":
    1. `create_deep_agent`'s own default `SummarizationMiddleware`
       really is unreachable as configured (85% of gpt-4o-mini's real
       `max_input_tokens`, confirmed as 128,000 by reading
       `ChatOpenAI(model="gpt-4o-mini").profile` directly - so the
       threshold is ~109k, above every per-call size ever observed).
       Fix: register our own `SummarizationMiddleware` with a much
       lower fixed-token trigger (8,000) via `create_deep_agent`'s
       `middleware=` argument, and exclude the unreachable default via
       `HarnessProfile(excluded_middleware=...)`.
    2. That exclusion is name-based, and a plain
       `SummarizationMiddleware(...)` instance reports the exact same
       `.name` as the built-in default (confirmed by reading
       `deepagents/middleware/summarization.py`) - so excluding "the
       default" silently excluded our replacement too, leaving zero
       summarization middleware active (confirmed: 0 events on a run
       whose real per-call tokens had already passed the trigger).
       Fix: a one-line subclass (`_LowTriggerSummarization`), which
       reports its own class name instead - deepagents' own docstring
       says subclassing is the intended way to avoid this collision.
    3. Even after fixing (1) and (2), summarization still didn't fire.
       Directly calling the middleware's own internal methods
       (`_should_summarize`, `_determine_cutoff_index`) on the real
       conversation confirmed it *was* deciding to fire - `agent.py`'s
       own detection code just wasn't seeing it. This version of
       deepagents implements summarization via `wrap_model_call`, not
       the older `before_model` hook: it only rewrites the *request*
       sent to the model, and never inserts a summary message into
       `state["messages"]` (confirmed by reading the middleware source
       - the only real trace as previously used it left in graph state
       is a private `_summarization_event` field). `agent.py` had been
       watching for a summary-shaped `HumanMessage` in
       `state["messages"]` - the older hook's behavior - so it could
       never see a real firing event even when one happened. This also
       retroactively explains an earlier crash during this same
       debugging session: on one run (pre-detection-fix, pre-prompt-fix),
       the agent called `read_excel_tool` on a path like
       `/conversation_history/session_....md` and crashed with
       "Unsupported file extension: .md" - concrete proof summarization
       *was* firing even while the old detection code reported zero
       events, and a sign the agent needs explicit guidance on which
       tool recovers that file. Fixed both: `agent.py` now reads
       `step["_summarization_event"]` directly (diffed against the last
       seen event, since it persists across steps once set) instead of
       scanning messages, and `SYSTEM_PROMPT` now tells the agent to use
       `read_file` (never `read_excel_tool`, which errors on non-
       spreadsheet paths) if it ever needs to reopen that file.
       A fourth, smaller thing surfaced along the way: the default
       `token_counter` (`count_tokens_approximately` with no `tools=`)
       only estimates conversation text, not the token cost of the 8
       tool schemas resent every call - a real gap given how much of
       this project's real per-call cost is schema overhead. Fixed by
       passing `token_counter=partial(count_tokens_approximately,
       tools=tools)` so the middleware's own estimate tracks what the
       model actually gets billed for, rather than just picking a lower
       trigger number to paper over the undercount.
    Verified end to end on the nurses file (cheapest sample) after all
    four fixes: **4 real summarization events fired** in one run
    (confirmed via the `_summarization_event` state field, each with a
    real `cutoff_index` and `conversation_history/*.md` offload path,
    not a text-pattern guess), the run completed with exit code 0 (no
    crash), and the final answer's numbers (two subscales, Cronbach's
    alpha 0.409 and 0.124, both correctly reported as poor reliability)
    checked out against the real tool output - no fabrication. **This is
    the first time in this project that context compaction has been
    shown to actually engage**, closing out the open item from the
    entries above. `results.json` now also records `summarization_events`
    per run (NFR-5/FR-8 style observability, alongside step count and
    tool latency).

### 8. Report and artifact output
Satisfies: FR-8.1, FR-8.2, FR-8.3; partially contributes to NFR-2

- `report.py: write_outputs()` writes `outputs/<handle_id>/<timestamp>/`
  per run: `report.md` (the final plain-language answer), `trace.log`
  (the exact tool-call/tool-result record, not just printed and lost),
  and `results.json` (the actual committed classifications/scales/groups/
  token-usage from `store.py` - the real numbers behind the prose, not
  just readable in trace text).
- `agent.py: run()` captures each message's `pretty_repr()` into a trace
  list as it prints, and calls `write_outputs()` at the end. No new
  dependency, no config flag - happens automatically every run, matching
  the spec's "MUST" wording.
- Verified on the nurses file: `report.md` matches the printed final
  answer, `trace.log` matches the printed trace exactly, `results.json`
  has the real numbers (22 classifications, 10 scales, 1 committed group,
  real token counts).
- Contributes to but doesn't fully satisfy NFR-2 (auditability): a
  per-run trace file now persists, but there's no structured, queryable
  audit trail across runs - each run is just its own folder.

### 9. Step count and per-tool latency
Satisfies: NFR-5

- `agent_tools.py: _timed` decorator, applied under `@tool` (not above
  it, so it times the plain function `@tool` reads to build its schema -
  `functools.wraps` keeps the name/docstring/signature intact for that)
  on all 8 tools. Records `{tool, seconds}` into `store.py: TOOL_CALLS`
  per call.
- `agent.py: run()` counts one step per LLM turn (reusing the existing
  token-counting loop - no separate pass needed), clears `TOOL_CALLS` at
  the start of each call so repeat `run()` calls in one process don't mix
  runs, and prints a step/tool-latency summary alongside the token
  summary.
- `report.py`'s `results.json` now also carries `step_count` and
  `tool_calls`, so these numbers are persisted per run, not just printed.
- Verified on the nurses file: 13 LLM turns, 24 tool calls, real per-tool
  timings (`read_excel_tool` ~16.6s including sandbox startup,
  `run_code_tool` ~11.2s total) - confirmed matching in both the printed
  summary and `results.json`.

### 8. Chunked reads and structural-issue detection
Satisfies: FR-1.4, FR-1.5 (partial - see scope note below)

- `tools.py: read_excel()` now checks file size before reading
  (`LARGE_FILE_BYTES`, 50MB). Under that, behavior is unchanged. Over it,
  CSVs are read via `pd.read_csv(..., chunksize=CHUNK_ROWS)` and
  concatenated, so peak memory grows chunk by chunk instead of all at
  once. Oversized Excel files raise a clear error instead of trying and
  failing later - pandas has no chunked Excel reader, so streaming Excel
  reads are out of scope for now (confirmed acceptable: no real oversized
  Excel file exists yet).
- `tools.py: _read_csv()` also fixes FR-1.5's encoding case: tries UTF-8,
  falls back to Latin-1 on `UnicodeDecodeError`. Which one worked is
  reported back as `encoding_used`.
- `tools.py: _detect_structural_issues()` is new, called from
  `read_excel()`. Split by how safe a fix is, same suggest-then-commit
  division already used for `classify_columns`/`infer_scale`/
  `group_items`: blank rows are dropped outright (no judgment call
  needed, count reported as `blank_rows_dropped`), while merged cells
  (via openpyxl) and a possible multi-row header are only flagged, never
  auto-fixed - deciding how to handle those needs the same kind of
  judgment as reverse-coding. The multi-row-header signal checks for
  `Unnamed: N` column names (pandas' own tell for a blank header cell,
  typical when only the left cell of a merged group carries text), not a
  guess based on row values.
- `agent_tools.py: read_excel_tool` surfaces `structural_issues` (and
  `encoding_used`, when not `utf-8`) in its result, and `prompts.py` was
  updated so the agent knows to read and act on them before analyzing.
- Fixed along the way: if `read_excel()` dropped blank rows or used a
  non-UTF-8 encoding, the host-side DataFrame no longer matched the raw
  bytes `read_excel_tool` was uploading to the sandbox - `run_code_tool`
  would have silently seen stale junk rows or hit the same encoding error
  pandas had already worked around. Fixed by re-uploading the cleaned
  DataFrame to the sandbox path instead of the raw file, the same
  temp-file-then-upload pattern `score_items_tool` already used.
- **Scope note, decided with the user during brainstorming**: chunking
  covers only the read + profile step; `classify_columns`/`infer_scale`/
  `group_items`/`score_items` still assume a fully-loaded DataFrame in
  `HANDLES`, unchanged - revisit only if a real large file makes that the
  actual bottleneck. FR-1.5's junk-row handling covers fully-blank rows
  only, not footer/total rows - no real messy file exists yet to validate
  a broader heuristic against.
- Verified with synthetic files (no real large/messy file existed to test
  against): a mid-file blank row correctly dropped and counted; a
  Latin-1-encoded file correctly decoded via fallback; a forced small
  `LARGE_FILE_BYTES` threshold correctly triggered the chunked path (200
  rows read correctly); an oversized `.xlsx` correctly rejected with a
  clear error; an `.xlsx` with a merged header cell correctly reported
  both `merged_cells` and `possible_multi_row_header`.

## Not built yet

- **FR-2**: parsing the analysis plan into an ordered, typed task list;
  asking a clarifying question by default when the plan is ambiguous.
- **FR-4.3**: persistent long-term memory across separate runs (schemas,
  cleaning routines, preferences, prior conclusions surviving between
  separate `uv run` invocations). The in-session part (FR-5/FR-6,
  context-budget tracking and compression) has shipped via `deepagents` -
  see section 7 above - though its actual benefit remains unconfirmed.
- **NFR-1, NFR-6**: deterministic re-runs and formal retry-with-backoff on
  tool failure. NFR-4 (chunking for large files) is now partially covered
  by section 8's read-step chunking, scoped down to just the read +
  profile step - see that section's scope note.

## Architecture decisions made along the way

- `agent.py` was refactored into four files once it grew past ~450 lines
  mixing unrelated concerns: `prompts.py` (the system prompt), `store.py`
  (the committed-state dicts - `HANDLES`, `CLASSIFICATIONS`, `SCALES`,
  `GROUPS`, `SANDBOX_PATHS` - plus the `json_safe` helper), `agent_tools.py`
  (every `@tool`-wrapped function and the sandbox lifecycle), and `agent.py`
  itself, now just the model + agent-framework wiring + `run()` (~110
  lines at the time of that split; ~170 after the `create_deep_agent`
  swap below added the harness-profile tool exclusion). No new
  abstractions or config layers - same plain functions and module-level
  dicts as before, just split by concern.
- `sandbox_tool.py: Runtime` now passes an explicit 20-minute `timeout` to
  `Sandbox.create()`. E2B's default is short and meant for one-off
  snippets; a real survey analysis sharing one sandbox across many tool
  calls (more Likert items = more calls) can outlive it, killing the
  sandbox mid-run with a `TimeoutException` - caught while testing against
  a 39-column file.
- LLM: OpenAI (`gpt-4o-mini`), the user's choice over Anthropic.
- Agent framework: LangChain's `create_agent` (originally built on the
  now-deprecated `create_react_agent`; migrated after LangChain flagged
  the deprecation), then swapped again on 2026-08-30 for `deepagents`'
  `create_deep_agent` (see below) once the memory/context-compression
  milestone was reached.
- Sandbox: E2B (`e2b_code_interpreter`), hand-rolled wrapper. No official
  LangChain+E2B integration exists, and LangChain's own sandbox package
  (Pyodide-based) is archived and deprecated.
- Memory/context-compression milestone framework (`requirements.md` §10):
  decided - adopted LangChain's `deepagents` package (`create_deep_agent`)
  instead of hand-rolling planning/summarization, rather than leaving it
  undecided. Shipped in `agent.py`. **2026-08-30, resolved**: replaced
  `create_deep_agent`'s own default `SummarizationMiddleware` (unreachable
  at its ~109k-token default trigger) with a custom lower-triggered one,
  registered through `middleware=` + a harness-profile exclusion of the
  default. Getting this working also required a one-line subclass (to
  dodge a `.name`-based exclusion collision between the default and the
  replacement) and fixing `agent.py`'s own detection code, which had been
  watching for a message shape (`before_model`'s summary `HumanMessage`)
  that this version of deepagents' `wrap_model_call`-based implementation
  never produces - the real signal is a private `_summarization_event`
  state field. See §7 above for the full account and verification (4 real
  events fired in one run, confirmed via that state field, no crash, real
  non-fabricated numbers in the final answer). Compaction is now confirmed
  to actually engage on this project, closing out what had been an open
  question since the `deepagents` swap.

## Where to look

- Full spec: `docs/requirements.md`.
- Code: `excel-analysis-agent-backend/` - `agent.py` wires the model and
  tools together and runs the loop; `agent_tools.py` has the `@tool`
  wrappers; `prompts.py` has the system prompt; `store.py` has the
  committed-state dicts; `tools.py` holds the plain functions each tool
  wraps; `report.py` writes each run's output files.
- Claude Code's session guidelines: `CLAUDE.md` at the repo root.
