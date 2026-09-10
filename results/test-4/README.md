# Test 4 — study-plan JSON wired in; run stopped by the token cap

**Date:** 2026-09-11 · **Files:** telemedicine CSV (292 x 58) + its study-plan JSON
**Run:** 29 LLM turns, 27 tool calls, **755,079 tokens**, 0 summarization events
**Model:** `gpt-5-mini` via OpenAI (`LLM_SOURCE=openai`), seed=42, reasoning_effort=medium
**Outcome:** `token_budget_exceeded` — stopped at the 750,000 ceiling before
reaching a final answer. **No analysis was completed.**

## Verdict

Two things worked and one thing broke.

**Worked — the study plan.** The agent read the JSON on demand and used it
as fact rather than inferring. It fetched `overview`, `variables`,
`analysis`, `column_map`, `reverse_coded` and `items` as separate slices,
never the whole file. It got all 13 reverse-coded items in one ~60-token
call, including 9 PSQ-18 items it could not plausibly have guessed — PSQ-18's
reverse set is not inferable from column names the way PSS-10's was in
test-3.

**Worked — the cap.** It fired exactly as designed at 755,079 tokens, wrote
the outputs, and returned an explanation rather than a fabricated answer.
Nothing was persisted to long-term memory, because a stopped run has no
validated conclusion to persist. This is the guardrail's first real firing
and it behaved correctly.

**Broke — every LLM turn issued exactly one tool call.**

```
tool calls per turn: {1: 29}
```

Test-3 issued 33 `infer_scale_tool` calls in a *single* parallel turn. Here
all 19 came one per round trip, each resending ~31,000 tokens of context.
That is the entire reason the cap was hit.

## This was not a loop

Worth stating plainly, because the cap's own message asks the question:

- 19 `infer_scale_tool` calls, **19 distinct columns**, zero repeats.
- `read_excel_tool` appears **once**, in the gate phase.
- 0 summarization events, so no context was ever evicted.

It was making real progress the whole time. It simply cannot afford to
make it one item per round trip.

## The arithmetic that kills it

The plan declares **42 Likert items**. At one item per LLM turn and ~31,000
input tokens resent per turn:

```
42 turns x ~31,000 = ~1,302,000 tokens for scale inference alone
```

before any grouping, scoring, or a single statistical test. The run got 19
of 42 items done — about 45% of just that one stage — for 755,079 tokens.

Raising `MAX_RUN_TOKENS` does not fix this. It buys a more expensive
failure.

## The fix is batching, not a bigger ceiling

Two changes, either of which would have let this run finish:

1. **A bulk scale-commit path.** When a study plan is loaded, per-item
   inference is redundant — the plan already states every item's anchor,
   options and reverse flag. One call could commit all 42 from
   `study_plan.items_for(...)` instead of 42 round trips. This is the
   direct fix and it is specific to having a plan.

2. **Make `infer_scale_tool` accept a list of columns.** The general fix,
   independent of plans, for the case where the model does not batch on its
   own. Test-3 shows it *can* batch; test-4 shows it does not reliably, so
   the tool should not depend on it choosing to.

Cost of the difference: 42 items in one turn is ~31k tokens. In 42 turns it
is ~1.3M. Same work, ~40x the bill.

## Also found: no retry on LLM calls (fixed)

The first attempt at this run, on Azure, died at turn 18 of ~20:

```
openai.RateLimitError: 429 ... rate_limit_exceeded (gpt-5-mini, eastus2)
```

392,713 tokens in 292s = ~80,700 tokens/min, over that deployment's quota.
`_with_retry` in `agent_tools.py` wraps **tools**, and its retryable set is
E2B's exceptions — nothing in this project had ever retried an *LLM* call,
and `max_retries` was unset (SDK default 2). A transient, explicitly
retryable error therefore destroyed a nearly-complete run.

Fixed in `_MODEL_KWARGS`: `max_retries=8`, `request_timeout=300`. The SDK
backs off exponentially and honours `Retry-After`.

`console_azure_ratelimited.log` is that first attempt, kept for the trace.

Note this 429 is a different failure from the OpenAI `insufficient_quota`
seen earlier in development: `rate_limit_exceeded` is transient and clears
on its own; `insufficient_quota` means a zero balance and never does.

## Other observations

- **The data dictionary is now the largest pinned block: ~2,521 tokens**,
  up from ~766 in test-3, because this CSV's headers are whole question
  sentences. It must carry exact column names for the agent to index the
  dataframe, so it cannot simply be truncated, but it duplicates much of
  what `column_map` serves on demand.
- **The gate phase has no access to the study plan.** It gets only
  `read_excel_tool`, `profile_tool` and `submit_plan_tool`, so it decides
  ambiguity without being able to read the declared outcome. Harmless here
  (`assume_and_state=True`), but a gate that could call
  `study_plan_tool(section='variables')` would know the outcome instead of
  assuming it.
- **Seven PSQI components are `method: "custom"`** with no algorithm in the
  JSON — the plan points at a `scoring.notes` that does not exist in the
  file. The agent was instructed to say so rather than invent banding; the
  run stopped before reaching that stage, so this is still untested.

## Status

| | test-3 | test-4 |
|---|---|---|
| dataset | 53 x 50, coded headers | 292 x 58, question-text headers |
| study plan | none | JSON, fetched in slices |
| reverse-coded items | 4, inferred (4/4 correct) | 13, **read as fact** |
| tool calls per turn | up to 33 | **always 1** |
| tokens | 428,064 | 755,079 |
| outcome | completed, verified correct | **stopped at the cap** |

The plan mechanism is sound. The batching is what needs fixing before
test-4 can be re-run.
