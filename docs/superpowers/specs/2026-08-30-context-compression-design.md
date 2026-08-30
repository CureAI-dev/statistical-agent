# Design: Context Compression via deepagents (FR-5, FR-6)

**Status:** Approved for planning
**Scope:** Fix in-session token-cost blowup on larger files. No persistent
long-term memory (FR-4.3) - deferred, see `docs/architecture-future-vision.md`.

## Problem

Every LLM call in `run()` resends the whole conversation so far, with no
compression. Measured on a 61-column file (~30 Likert items): total run
cost ~1-1.2M tokens vs. ~50-150k for a 10-item file - about 10-20x more
cost for 3x more items, because history grows unbounded within a run.
This is exactly FR-5/FR-6 ("track token cost against a budget" /
"compress older turns once a threshold is crossed") - currently
unimplemented (confirmed via grep: no memory tiers, no budget tracker, no
compression exist in the codebase).

## Approach

Adopt the `deepagents` package. Replace `langchain.agents.create_agent`
with `deepagents.create_deep_agent` in `agent.py`. Everything else -
`agent_tools.py`, `tools.py`, `store.py`, `prompts.py`, the sandbox
lifecycle - is untouched. Our 8 tools are plain `@tool`-decorated
LangChain functions, the same interface `create_deep_agent(tools=...)`
expects - no tool changes needed.

`create_deep_agent` includes `SummarizationMiddleware` automatically:
once a single call's context hits ~85% of the model's max input tokens
(~109k for gpt-4o-mini's 128k window), it summarizes everything except
the most recent ~10% of tokens into a generated summary, and the original
messages are preserved via a configurable backend (not lost, just moved
out of the live context). This directly caps the runaway per-call context
size that drove the 1M-token run.

## Decisions

- **Backend for compacted-away messages:** `StateBackend` (in-memory, no
  disk writes) - matches the "session-only, no persistence" scope and
  keeps NFR-3 (no unapproved host writes) trivially true.
- **Built-in filesystem/subagent tools:** `create_deep_agent` also adds
  `ls`/`read_file`/`write_file`/`edit_file`/`delete`/`glob`/`grep`/
  `execute` and a subagent `task` tool by default. These send their
  schemas on every call whether used or not - pure overhead for this
  project, since all data lives in the E2B sandbox, not an agent-managed
  filesystem. Exclude them via `excluded_tools` so we don't pay tokens
  for tools that never get called.
- **Compression threshold:** leave at the library default (~85%) rather
  than the spec's 70% - no evidence yet that 85% is too late in practice;
  revisit if a run still blows up before compression kicks in.
- **Model wiring:** pass the existing `ChatOpenAI(model="gpt-4o-mini")`
  instance as `model=`, same as today's `create_agent` call. If
  `create_deep_agent` rejects a model instance and wants a string
  (`"openai:gpt-4o-mini"`), fall back to that - confirm during
  implementation.

## Out of scope

- Persistent long-term memory across separate `uv run` invocations
  (FR-4.3) - no schemas/preferences/conclusions survive between runs.
- The planning/todo tool `deepagents` also ships (maps to FR-2.1) - not
  used this milestone.
- LangGraph checkpointer persistence, LangSmith tracing, progressive tool
  disclosure - all deferred, see `docs/architecture-future-vision.md` for
  why.
- NFR-6 (retry-with-backoff on tool failure) - separate future item.

## Risks

- `deepagents` is a fast-moving 2026 package; not yet verified against
  this project's Python 3.14. Check `uv add deepagents` installs cleanly
  before writing any integration code.
- `run()`'s trace-printing loop (`agent_graph.stream(..., stream_mode=
  "values")`) assumes a certain message-list shape per step. Verify
  `create_deep_agent`'s compiled graph streams the same way before
  assuming the existing loop still works unmodified.

## Testing plan

1. Cheap sanity check: re-run the small nurses file smoke test. Confirm
   the pipeline still completes correctly (classification, scale
   inference, grouping, scoring, chi-square, logistic regression) with no
   new errors.
2. Real test: re-run the 61-column stress-test file again. Compare total
   token usage against the ~1-1.2M token baseline from the pre-fix runs
   (recorded in `docs/progress.md`). Confirm no fabricated results
   (already guarded by the existing `SYSTEM_PROMPT` rule) and check
   whether the summarization actually engaged (visible in the trace or by
   the per-call input token counts staying capped instead of climbing
   unbounded).
