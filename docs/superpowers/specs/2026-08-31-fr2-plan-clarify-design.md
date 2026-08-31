# Design: Plan Parsing + Default Clarifying Question (FR-2)

**Status:** Approved for planning
**Scope:** FR-2.1 (parse the analysis request into an ordered, typed task
list), FR-2.2 (ask a single targeted clarifying question by default when
the request is ambiguous, with an `assume_and_state` flag to skip
asking), FR-2.3 (re-plan mid-task if an observation invalidates the
current plan).

## Problem

No plan parsing or clarifying-question step exists today - confirmed by
grep, zero code for either. `agent.py`'s `run()` goes straight from the
user's question to tool calls via the `deepagents` ReAct loop, with no
ambiguity check and no explicit task list. For survey data specifically
(this project's specialty), requests are often ambiguous - no stated
outcome variable, several equally valid subgroup readings - and FR-2.2
requires the agent ask once rather than guess. The spec's acceptance
criteria (§9, items 1-2) assume this step exists; it doesn't.

## Approach

Split `run()` into two phases instead of one:

- **Phase 1 (gate).** A restricted-tool agent call - tools limited to
  `read_excel_tool` and `profile_tool` only. It loads the file, profiles
  it, and judges ambiguous-or-not against the real question and the
  file's real columns. It ends its turn with either a clarifying
  question, or (an assumption statement, if `assume_and_state=True`)
  plus a typed task list. Because no scoring/testing/mutating tool is
  even present in its tool list, it cannot proceed into real analysis no
  matter what it decides to do - a structural guarantee, not reliance on
  the model following prompt instructions correctly. This project has
  repeated documented history of prompt-only guidance failing under the
  small model in use (`gpt-4o-mini`): a 297-call retry loop, ignored tool
  output requiring a prompt patch - see `docs/progress.md` §7.
- **Phase 2 (real work).** Runs only if phase 1 did not ask a question.
  The existing full-tool agent, unchanged, seeded with phase 1's task
  list and already-loaded `handle_id` in its opening message, so it does
  not re-read the file or redo planning.

`store.py`'s module-level dicts (`HANDLES`, `SANDBOX_PATHS`, etc.) and
the E2B sandbox (`agent_tools.py: _get_sandbox()`) are already
process-global singletons, so phase 1's loaded file and open sandbox
carry into phase 2 with no new plumbing - just don't call
`close_sandbox()` until `run()` actually finishes, whichever phase it
stops at.

## Decisions

- **Task shape:** a plain dict, matching this project's existing style
  (no classes anywhere in `tools.py`/`store.py`):
  `{step: str, description: str, status: "pending"|"in_progress"|"done"}`.
- **`step` vocabulary:** project-specific stages (`classify`,
  `infer_scale`, `group`, `score`, `test`, `report`) rather than the
  spec's own generic example wording (load/clean/aggregate/visualize/
  summarize). This matches what the agent's actual tools do and is more
  useful for FR-2.3's re-plan case, since it references tool-shaped
  stages the agent can act on directly. `load`/`profile` are deliberately
  not stages in this list - phase 1 already executes both before the
  list is produced, so including them would just describe work already
  done.
- **`run()` return shape** changes from a plain string to a dict:
  `{"status": "needs_clarification", "question": str}` or
  `{"status": "done", "answer": str}`. This is a breaking change to the
  one existing caller (`main.py`) - acceptable, since nothing else calls
  `run()` yet.
- **`assume_and_state: bool = False`** - new `run()` parameter, read only
  by phase 1's prompt.

## Error handling

- If phase 1 itself errors (bad file, tool crash), the same crash path
  as today applies - no new handling added, out of scope for this
  feature.
- If phase 1 finishes without clearly producing either a task list or a
  clarifying question (the model goes off script), treat the output as a
  clarifying question by default - fail toward asking rather than
  silently guessing, consistent with this project's documented
  small-model reliability gaps.
- **FR-2.3 (re-plan mid-task):** no new tool. The phase-2 system prompt
  tells the agent that if a tool result contradicts a task-list
  assumption (e.g. an expected column is missing), it must state the
  revision plainly in its next message before continuing, rather than
  push through silently. Text-level, the cheapest fit given the task
  list itself lives in message content, not a separately tracked mutable
  object.

## Out of scope

- **True mid-run pause** (LangGraph `interrupt()` + a checkpointer) -
  considered and rejected for this milestone. It would turn `run()` from
  a one-shot call into a resumable, multi-turn one and change
  `main.py`'s calling shape, for a guarantee the two-phase design already
  gets more cheaply.
- **Reusing `deepagents`/`langchain`'s built-in `write_todos` tool**
  (`TodoListMiddleware`) for the task list - checked its schema directly
  (`langchain/agents/middleware/todo.py`): `{content: str, status}` only,
  no type field, and the tool's own description leaves using it entirely
  to model discretion ("skip for tasks under 3 steps"). Doesn't satisfy
  FR-2.1's "typed" requirement or give a structural guarantee, so a
  hand-rolled dict is used instead.
- **Persistent long-term memory** (FR-4.3) - unrelated, already tracked
  separately as not built.
- **NFR-6** (retry-with-backoff on tool failure) - separate future item,
  already noted as unbuilt in `docs/progress.md`.

## Risks

- Two agent calls per run instead of one adds a fixed token/latency cost
  (phase 1's own model call plus its tool schemas) even on unambiguous
  requests. Small relative to typical full-run cost (real runs range
  ~50k-1.7M tokens per `docs/progress.md`), but not zero.
- Phase 1 only sees `read_excel_tool`/`profile_tool` output, not the
  deeper column classification phase 2 would eventually produce, so some
  ambiguity is only detectable mid-analysis (e.g. a construct that turns
  out to split into two subscales unexpectedly). Out of scope for phase
  1 by design - it catches question-level ambiguity (no stated outcome,
  multiple valid readings), not deep structural ambiguity. FR-2.3's
  re-plan step is the intended catch for the latter.
- The six-stage task-list vocabulary may not fit every file shape - e.g.
  the fully pre-scored file case already seen in `docs/progress.md`,
  which skips `classify`/`infer_scale`/`group` entirely. The prompt must
  allow the model to omit non-applicable stages rather than force all six
  into every task list.

## Testing plan

1. Ambiguous question, `assume_and_state=False` (e.g. no stated
   outcome/metric) - expect `{"status": "needs_clarification", ...}`;
   confirm via trace that only `read_excel_tool` and `profile_tool` ran,
   zero sandbox mutation, zero calls to `classify_columns_tool`,
   `run_code_tool`, or any other phase-2-only tool.
2. Same ambiguous question, `assume_and_state=True` - expect a stated
   assumption line followed by a normal full run to completion.
3. Clear question against the nurses file (known-good baseline in
   `docs/progress.md`) - expect the unchanged full pipeline
   (classification, scale inference, grouping, scoring, chi-square/
   regression) with same-shape final numbers as past verified runs, now
   with a visible task list in the trace.
4. Update `main.py`'s smoke test to branch on the new `{"status": ...}`
   return shape instead of assuming a plain string.
