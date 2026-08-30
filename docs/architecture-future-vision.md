# Architecture — Future/Production Vision (not current scope)

**Status:** Reference only. Not being built now. Saved 2026-08-30 for future
lookup when the project has the budget/need for a full production
deployment. Do not treat this as a description of what exists — check
`docs/progress.md` for that.

This was proposed as a "production-grade" architecture for this agent,
built on the Deep Agents SDK + LangGraph. Most of it is good direction for
a later, funded, higher-scale phase, but overshoots this project's current
stage: a cost-conscious v1 (see CLAUDE.md's "Simplicity first" rule) with
8 tools, not 30+, and no budget for hosted third-party services.

What was scoped OUT of the current memory/compression milestone (2026-08)
and why - revisit each when the situation actually calls for it:

- **Progressive tool disclosure** (dynamic tool hiding via
  `wrap_model_call`/`request.override`) - solves prompt bloat from a large
  tool catalog (30+ tools). This project has 8. Revisit only if the tool
  count grows that large.
- **LangGraph checkpointer persistence** - for resuming crashed/long runs
  across process restarts, needs a real persistence backend
  (Postgres/SQLite). Doesn't itself make numeric results more
  deterministic (that already holds - the sandbox runs the same code on
  the same data). Revisit if runs need to survive a crash mid-analysis.
- **LangSmith for auditability** - hosted third-party observability
  service, separate signup and cost. Revisit once there's budget and a
  real need to inspect traces outside the terminal.
- **Persistent filesystem-backed long-term memory (KV store)** - this is
  FR-4.3 in `docs/requirements.md`: schemas seen before, validated
  cleaning routines, user preferences, prior conclusions, surviving across
  separate `uv run` invocations. Deliberately deferred - the 2026-08
  milestone only fixes in-session context growth (FR-5/FR-6), not
  cross-session memory. Revisit when the agent needs to remember things
  between separate runs, not just within one.
- **70% context-compression trigger** - `docs/requirements.md` specifies
  70%; the deepagents package actually adopted (see
  `docs/progress.md`) defaults to 85%. Left at the library default for
  now; revisit if spec-exact behavior is ever required (the middleware
  takes a configurable threshold).

---

## Original document as presented

**Component:** Autonomous Coding & Data Analysis Agent (Deep Agents + LangGraph SDK)
**Target File Reference:** `architecture.md`

---

### 1. Executive Summary & Design Philosophy
This document outlines the architecture for an autonomous spreadsheet analysis agent built on top of the **Deep Agents SDK** and **LangGraph**. Designed to fulfill the requirements of the Excel Analysis Agent (SRS v1.0), the system operates via a recursive perceive-plan-act-reflect loop, maintaining strict context budgeting, secure sandboxed execution, and domain-specific routing for both standard financial/tabular modeling and advanced survey/psychometric analysis.

---

### 2. High-Level Architecture

```
                                +---------------------------------------------------+
        User Request            |                   ORCHESTRATOR                    |
        + Analysis Plan  ──────►|          (Deep Agent / LangGraph StateGraph)      |
                                +---------+------------------+----------+-----------+
                                          |                  |          |
                                          v                  v          v
                                  +---------------+   +--------------+  +-------------------+
                                  | CONTEXT MGR   |   | TOOL RUNTIME |  | MEMORY SUBSYSTEM  |
                                  | - Token Budget|   | - Pyodide /  |  | - Working Mem     |
                                  | - Assembly    |   |   Docker Env |  | - Session State   |
                                  | - Compression |   | - Pandas/SciPy|  | - Persistent Store|
                                  +-------+-------+   +------+-------+  +---------+---------+
                                          |                  |                    |
                                          +--------- summaries / handles ---------+
                                                             |
                                                             v
                                                    Report + Artifacts + Trace
```

---

### 3. Core Component Specifications

#### 3.1 Orchestration & Agent Harness
* **Framework:** LangGraph state machine wrapped in the Deep Agents SDK.
* **Execution Flow:**
  1. **Ingest & Profile:** Inspect file dimensions, column names, null counts, and inferred data types without loading full files into the LLM context window.
  2. **Plan Generation:** Parse user intent into an ordered, typed task list stored in session state.
  3. **Tool Execution Loop:** Iteratively call tools, evaluate outputs, handle tracebacks, and self-correct.
  4. **Verification & Reporting:** Reconcile totals, format findings with cited numbers, and generate output artifacts.

#### 3.2 Tool Suite & Sandboxed Execution
All data transformation and computation must occur inside an isolated execution environment (`run_code`) using `pandas`, `numpy`, `openpyxl`, `scipy`, and `statsmodels`.
* **Data Handles:** Large datasets remain in the runtime memory store; only schemas, metadata, and compact summaries enter the model's context window.
* **Dynamic Tool Disclosure:** Use middleware (`wrap_model_call` with `request.override`) to implement progressive tool discovery for large tool suites (30+ tools), keeping prompts compact and performant.

#### 3.3 Statistical & Survey Analysis Engine
To handle both general business spreadsheets and complex questionnaire workbooks (Likert scales, subscale grouping, and regression):
* **Column Profiling & Classification:** Automatically tag columns as *Likert/ordinal*, *categorical*, *open-ended text*, or *identifier*.
* **Automated Test Selection:** Dynamically match variables to statistical tests based on distribution and data types:
  * *Continuous & Normal:* Independent t-test, One-way ANOVA, Pearson correlation, Linear regression.
  * *Continuous & Non-Normal / Ordinal:* Mann–Whitney U test, Kruskal–Wallis test, Spearman correlation.
  * *Categorical:* Chi-square test (or Fisher's exact test for small expected cell counts).
  * *Binary Outcomes:* Binary logistic regression.
* **Incremental Transformation:** Apply data recoding, reverse-scoring, and subscale aggregation progressively against working copies without mutating the source file.

#### 3.4 Memory & Context Management
* **Working Memory:** Active dataframe handles, current scratchpad, and latest step observations.
* **Session Memory:** Running plan, completed steps, and rolling summary.
* **Long-Term Memory:** Filesystem-backed key-value store persisting schemas, validated cleaning routines, and user preferences across sessions.
* **Context Compression:** When context reaches 70% of the token budget, older turns are compressed into a structured rolling summary (`Done / Findings / State / Next`) while re-hydrating details from memory handles as needed.

---

### 4. Non-Functional & Operational Requirements

* **Reliability (NFR-1):** Deterministic re-runs with fixed checkpoints via LangGraph checkpointer persistence.
* **Auditability (NFR-2):** Full execution traces logged to **LangSmith** for every run.
* **Safety (NFR-3):** Sandboxed code execution with no arbitrary host file writes or unapproved network egress.
* **Observability (NFR-5):** Real-time emission of token usage, step count, and tool latencies.

---

**Relevant docs:**
- [Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)
- [LangGraph Documentation](https://docs.langchain.com/oss/python/langgraph/overview)
- [Progressive Tool Disclosure](https://support.langchain.com/articles/8488719552-progressive-tool-disclosure-with-deep-agents)
- [LangGraph Checkpointing](https://docs.langchain.com/oss/python/langgraph/checkpointers)
