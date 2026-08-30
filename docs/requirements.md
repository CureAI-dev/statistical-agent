# Requirements — Excel Analysis Agent

**Document type:** Software Requirements Specification (SRS)
**Component:** Autonomous coding agent for spreadsheet analysis
**Status:** Draft v1.0

---

## 1. Overview

### 1.1 Purpose
Build an autonomous coding agent that accepts a specific Excel file (`.xlsx` / `.xls` / `.xlsm` / `.csv`) together with a natural-language or structured **analysis plan**, and then autonomously reads, reasons over, computes, and reports results — running a full perceive → plan → act → observe → reflect loop without step-by-step human hand-holding.

### 1.2 Design principle
The agent should behave like a general-purpose reasoning agent (tool use + memory + iterative planning), not a single-shot script. It decides *which* tools to call, *when* to call them, keeps state across steps, and manages a bounded context window by summarizing and compressing as work accumulates.

### 1.3 Definitions
| Term | Meaning |
|------|---------|
| **Analysis plan** | The user's stated goal(s): metrics, transformations, aggregations, charts, or questions to answer. |
| **Working memory** | Short-lived state for the current task (loaded data handles, intermediate results, scratchpad). |
| **Long-term memory** | Persistent store surviving across turns/sessions (schemas, prior findings, user preferences). |
| **Context window** | The token budget passed to the LLM on each step. |
| **Compression** | Lossy/lossless summarization applied when context approaches its budget. |

---

## 2. Scope

### 2.1 In scope
- Ingesting one target Excel/CSV file per task (multi-sheet supported).
- Interpreting an analysis plan into concrete executable steps.
- Executing data operations in a sandboxed code runtime.
- Maintaining memory and a self-managing context window.
- Producing a written analysis plus optional output artifacts (tables, charts, cleaned files).

### 2.2 Out of scope (v1)
- Real-time streaming data sources.
- Editing the source file in place (agent works on copies).
- Multi-user concurrency on a single task.

---

## 3. Inputs & Outputs

### 3.1 Inputs
1. **Target file** — path or upload of the spreadsheet to analyze.
2. **Analysis plan** — free text ("find monthly revenue trend and flag outliers") or structured JSON (list of typed steps).
3. **Optional config** — output format, verbosity, row/size limits, allowed tools.

### 3.2 Outputs
1. **Analysis report** — narrative findings with cited numbers.
2. **Artifacts** — derived spreadsheets, charts (PNG/SVG), or a results `.json`.
3. **Execution trace** — ordered log of tool calls, reasoning summaries, and errors for auditability.

---

## 4. Functional Requirements

### 4.1 Excel Ingestion & Access
- **FR-1.1** The agent MUST open `.xlsx`, `.xls`, `.xlsm`, and `.csv` files.
- **FR-1.2** It MUST enumerate all sheets, and for each sheet report dimensions, column headers, and inferred dtypes.
- **FR-1.3** It MUST profile the data (row count, null counts, numeric ranges, sample rows) **before** loading everything into context — headers and a small sample go to the model; the full table stays in the runtime.
- **FR-1.4** It MUST handle large files by lazy/chunked reads and never load a full multi-MB sheet into the LLM context.
- **FR-1.5** It MUST detect and report structural issues (merged cells, multi-row headers, junk rows, encoding problems) and attempt sensible normalization.

### 4.2 Analysis Plan Interpretation
- **FR-2.1** The agent MUST parse the analysis plan into an ordered, typed task list (e.g. `load → clean → aggregate → visualize → summarize`).
- **FR-2.2** When the plan is ambiguous or underspecified, the agent MUST **ask a single targeted clarifying question by default**. An `assume_and_state` config flag MAY switch this to making a reasonable, explicitly stated assumption and proceeding without asking. (Resolves Open Question §10, item 1.)
- **FR-2.3** It MUST re-plan mid-task if an observation invalidates the current plan (e.g. an expected column is missing).

### 4.3 Tool Suite
The agent operates through explicit tools. At minimum:

| Tool | Purpose |
|------|---------|
| `read_excel(path, sheet, range?)` | Load a sheet or range into the runtime as a dataframe handle. |
| `profile(handle)` | Return schema, dtypes, nulls, and summary stats. |
| `run_code(code)` | Execute Python (pandas/numpy/openpyxl) in a sandbox against loaded handles. |
| `query(handle, expr)` | Filter/aggregate without writing full code. |
| `plot(spec)` | Produce a chart artifact. |
| `write_output(data, format)` | Persist a result artifact to the outputs directory. |
| `memory_read/write(key, value)` | Access long-term memory. |

- **FR-3.1** Each tool MUST return a compact, model-readable result (a summary + reference handle), NOT the full raw data blob.
- **FR-3.2** Tool calls MUST be logged with inputs, truncated outputs, and status.
- **FR-3.3** `run_code` MUST execute in an isolated sandbox (no host filesystem write outside a scratch dir, no arbitrary network).

### 4.4 Memory System
Three tiers:

**Working memory (per step)**
- **FR-4.1** Holds active dataframe handles, the current scratchpad, and the latest observations.

**Session memory (per task)**
- **FR-4.2** Holds the running plan, completed steps, intermediate results, and a rolling summary of what has been done and learned.

**Long-term memory (persistent)**
- **FR-4.3** Stores durable facts across sessions: file schemas seen before, validated cleaning routines, user output preferences, and prior conclusions.
- **FR-4.4** MUST be keyed and queryable so the agent can retrieve only what is relevant rather than dumping the whole store into context.
- **FR-4.5** Large data objects live in the runtime/store and are referenced by handle; only handles + summaries enter the context window.

### 4.5 Context Window Management
- **FR-5.1** The agent MUST track the token cost of the assembled context on every step against a configured budget.
- **FR-5.2** Context is assembled by priority: system instructions → current plan → recent observations → relevant retrieved memory → rolling summary. Lowest-priority items are dropped first.
- **FR-5.3** Raw tool outputs MUST be truncated/summarized before entering context; full outputs remain retrievable by handle.
- **FR-5.4** The agent MUST never let a single tool result (e.g. a 100k-row dump) blow the budget — it summarizes or samples instead.

### 4.6 Context Compression
- **FR-6.1** When context reaches a threshold (e.g. 70% of budget), the agent MUST compress older turns into a concise **rolling summary** that preserves: decisions made, results obtained, open questions, and active handles.
- **FR-6.2** Compression MUST be structured (e.g. `Done / Findings / State / Next`) so nothing load-bearing is lost.
- **FR-6.3** Superseded intermediate results MUST be evictable once summarized.
- **FR-6.4** The agent MUST be able to "rehydrate" a detail by re-reading a handle from memory rather than keeping it verbatim in context.

### 4.7 Agentic Reasoning Loop
- **FR-7.1** The agent runs an iterative loop: **Observe** (state + last result) → **Plan/Decide** (next tool or finish) → **Act** (call tool) → **Reflect** (evaluate, update summary) — repeating until the plan is satisfied or a stop condition is met.
- **FR-7.2** It MUST self-verify results (sanity checks, reconciliation of totals) before reporting.
- **FR-7.3** It MUST enforce a max-step / max-cost guardrail and stop gracefully with partial results if exceeded.

### 4.8 Output & Reporting
- **FR-8.1** The final report MUST state findings in plain language with the exact numbers they derive from.
- **FR-8.2** Every quantitative claim MUST be traceable to a logged computation.
- **FR-8.3** Artifacts MUST be written to a designated outputs directory and referenced in the report.

### 4.9 Questionnaire / Survey Analysis Mode

When the target file is questionnaire/survey data — a wide table where **each column is a question/item** and **each row is a respondent**, with cells holding either Likert-type responses or free-text (open-ended) answers — the agent MUST enter a dedicated survey-analysis workflow.

**Detection & structure**
- **FR-9.1** The agent MUST recognise questionnaire-shaped data and classify every column as one of: *Likert/ordinal item*, *categorical item*, *open-ended text*, or *identifier/metadata*.
- **FR-9.2** For each Likert item, the agent MUST **decide the scale itself** — inferring the number of points (e.g. 3/5/7), the label→score mapping (e.g. "Strongly disagree"→1 … "Strongly agree"→5), and whether the item is reverse-coded — and MUST state the inferred scale plus its confidence. The user MAY override.
- **FR-9.3** Open-ended columns MUST be separated from the scored items and handled distinctly (excluded from numeric scoring; optionally categorised or flagged for text analysis).

**Grouping**
- **FR-9.4** The agent MUST **decide how items group** into constructs/subscales — using column-name and semantic similarity, any user-supplied mapping, and/or the correlation structure of the responses — and MUST report the grouping with its rationale so the user can confirm or adjust.

**Scoring**
- **FR-9.5** The agent MUST map Likert responses to numeric scores per its inferred scale, apply reverse-coding where detected, and compute per-respondent and per-group (subscale) scores.
- **FR-9.6** Scale reliability (e.g. Cronbach's α) SHOULD be reported per group so the user can judge whether the grouping holds together.

**Code-generate → execute → transform, step by step**
- **FR-9.7** Data transformation MUST be carried out by the agent **generating code and executing it incrementally against a working copy** of the data. Each step (recode labels → reverse-score → aggregate to subscale → derive outcome variable) is generated, run, and **verified before the next step**, so the working dataset is modified progressively rather than in one opaque pass. The **source file is never mutated** (per NFR-3).
- **FR-9.8** Only once the dataset is fully scored and grouped does the agent proceed to the analysis/testing phase.

**Statistical testing**
- **FR-9.9** The agent MUST select and run tests appropriate to the variable types and the analysis plan, at minimum supporting:
  - **Chi-square** tests of association between categorical/ordinal items.
  - **Logistic regression** for binary outcomes (predictors = item or subscale scores, or categorical factors).
  - Supporting descriptives, cross-tabs, and effect sizes.
- **FR-9.10** For every test the report MUST state the test chosen, why it fits the data, the assumptions and whether they hold, and the results (coefficients / odds ratios, test statistics, p-values, effect sizes) with a plain-language interpretation.

**Tools added for this mode**
| Tool | Purpose |
|------|---------|
| `classify_columns(handle)` | Tag each column as Likert / categorical / open-ended / identifier. |
| `infer_scale(handle, col)` | Decide points, label→score map, and reverse-coding for a Likert item. |
| `group_items(handle)` | Propose construct/subscale grouping with rationale. |
| `score_items(handle, mapping)` | Apply (reverse-)coding and compute subscale scores into the working copy. |
| `run_stat_test(handle, spec)` | Run chi-square / logistic regression / descriptives and return a compact result. |

---

## 5. Non-Functional Requirements

- **NFR-1 Reliability:** Deterministic re-runs on the same file+plan should yield the same numeric results.
- **NFR-2 Auditability:** Full execution trace retained for every task.
- **NFR-3 Safety:** Sandboxed execution; no destructive ops on the source file; no unapproved network egress.
- **NFR-4 Scalability:** Handle files up to a configurable row/size limit via chunking without OOM.
- **NFR-5 Observability:** Token usage, step count, and tool latency emitted per task.
- **NFR-6 Graceful degradation:** On tool failure, retry with backoff, then re-plan, then report partial results — never crash silently.

---

## 6. Architecture Overview

```
                +--------------------------------------------------+
   analysis     |                  ORCHESTRATOR                    |
   plan  ─────► |   (agentic loop: observe→plan→act→reflect)       |
   + file       +----------+------------------+----------+---------+
                           |                  |          |
                           v                  v          v
                   +---------------+   +--------------+  +-------------------+
                   | CONTEXT MGR   |   | TOOL RUNTIME |  | MEMORY SUBSYSTEM  |
                   | - budget      |   | - read_excel |  | - working        |
                   | - assembly    |   | - run_code   |  | - session        |
                   | - compression |   | - plot/write |  | - long-term (kv) |
                   +-------+-------+    +------+-------+  +---------+---------+
                           |                  |                    |
                           +--------- summaries / handles ---------+
                                              |
                                              v
                                     Report + Artifacts + Trace
```

### 6.1 Data flow
1. File is profiled; schema + sample enter context, full data stays in runtime by handle.
2. Plan is parsed into steps and stored in session memory.
3. Loop executes each step via tools; observations are summarized into the rolling state.
4. Context manager compresses when the budget tightens.
5. Results are verified, then written out with a trace.

---

## 7. Error Handling & Recovery

| Failure | Behavior |
|---------|----------|
| File unreadable / corrupt | Report clearly; attempt alternate parser; stop if impossible. |
| Missing expected column | Re-profile, re-plan, and note the deviation in the report. |
| Code execution error | Capture traceback, self-correct once or twice, then escalate. |
| Context overflow | Trigger compression; if still over, drop lowest-priority context. |
| Step/cost cap hit | Halt, summarize progress, return partial results. |

---

## 8. Suggested Tech Stack (non-binding)

- **Runtime:** Python sandbox (pandas, numpy, openpyxl, matplotlib).
- **Agent framework:** LLM with tool/function calling + an orchestration loop.
- **Memory:** in-process dict for working/session; a key-value or vector store for long-term retrieval.
- **Context accounting:** a tokenizer-based budget tracker.

---

## 9. Acceptance Criteria

The agent is considered complete for v1 when it can, unattended:
1. Accept a real multi-sheet Excel file plus a free-text analysis plan.
2. Profile the data without loading it wholesale into context.
3. Execute the plan through tool calls, self-correcting on at least one induced error.
4. Stay within its context budget on a long task by compressing history.
5. Produce a correct, number-traceable report plus at least one artifact.
6. Emit a complete execution trace and token/step metrics.
7. Given a questionnaire workbook, auto-detect the Likert scale(s), group items into subscales, score them by generating and running code step by step against a working copy, and run at least one chi-square and one logistic-regression test — reporting each test's choice, assumptions, and results.

---

## 10. Open Questions
- ~~Should the agent ask clarifying questions by default, or assume-and-state?~~ **Resolved:** the agent **asks a clarifying question by default**; an `assume_and_state` flag flips it to assume-and-state. See FR-2.2.
- Long-term memory scope: per-user, per-file-schema, or both?
- Maximum file size before mandatory chunking?
- **Deep Agents SDK for the memory/context-compression milestone (FR-4/5/6):** LangChain's `deepagents` package ships a built-in planning/todo tool (maps to FR-2.1) and context-summarization middleware (maps to FR-6's 70%-threshold compression). **Resolved:** adopted — `agent.py` now builds the agent with `deepagents.create_deep_agent` instead of hand-rolling this. Whether it actually delivers on FR-6 is unverified rather than confirmed: see `docs/progress.md` §7's 2026-08-30 re-verification, which was derailed by an unrelated retry-loop bug before the summarization trigger was ever reached, and separately found the trigger threshold itself (~109k tokens) sits well above every per-call token size this project's workload has produced so far (largest observed: 74.4k).
