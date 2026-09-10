# Test 4 — study plan + batched scale commit: first completed run on a raw survey export

**Date:** 2026-09-11 · **Files:** telemedicine CSV (292 x 58) + its study-plan JSON
**Run:** 22 LLM turns, 20 tool calls, **615,179 tokens** (82% of the 750,000 cap),
0 summarization events, exit 0
**Model:** `gpt-5-mini` via OpenAI (`LLM_SOURCE=openai`), seed=42, reasoning_effort=medium
**Outcome:** completed. Every reported statistic independently reproduced from the raw
CSV **to 10 decimal places**.

`README_attempt1_capped.md` documents the earlier attempt that the token cap
stopped; this file supersedes it.

## Verified results

Recomputed from the raw CSV using the plan's own anchors and reverse flags:

| subscale | n items | agent | recomputed | |
|---|---|---|---|---|
| Patient_satisfaction (PSQ-18) | 18 | 0.883 | 0.883 | MATCH |
| PSS10 | 10 | 0.869 | 0.869 | MATCH |
| PSQI | 14 | 0.882 | 0.882 | MATCH |

| Spearman | agent | recomputed | |
|---|---|---|---|
| PSS10 ~ Patient_satisfaction | rho = 0.6926553648 | 0.6926553648 | MATCH |
| Patient_satisfaction ~ PSQI | rho = 0.7319640116 | 0.7319640116 | MATCH |
| PSS10 ~ PSQI | rho = 0.6547939320 | 0.6547939320 | MATCH |

All three correlations p < 1e-36. It also disclosed `n_incomplete = 18` for the
PSQI group, which is correct — that many respondents are missing at least one
PSQI item, so the reliability figure rests on fewer people than the score does.

**The study's primary hypothesis is supported in this data**: perceived stress,
sleep quality and patient satisfaction are all strongly associated.

## What made this run possible

The previous attempt spent 755,079 tokens, scaled 19 of 42 items, and was
stopped by the cap before running a single test. The difference is one tool.

**`infer_scales_tool`** commits many items in a single call. With a study plan
loaded, `from_plan=True` takes every anchor, label->score map and reverse flag
straight from the plan - stated design facts, so nothing is inferred and the
model does not have to retype them.

```
n_committed: 42   failed: 0   (~1,963 tokens, ONE call)
reverse-coded (13): PSQ18_1,2,3,5,6,8,11,15,18 + PSS10_4,5,7,8
```

42 round trips at ~31,000 resent tokens each became one. Note this was never a
framework limit: nothing sets `parallel_tool_calls`, deepagents does not force
sequential execution, and test-3 issued 33 calls in a single turn. The model
simply chose one at a time, and the fix is to stop depending on that choice.
(deepagents ships a `<use_parallel_tool_calls>` nudge in its *Anthropic*
harness profiles and none for OpenAI.)

**Bug caught while building it**: the first version failed all 42 items with
"not a column in this file". The plan names items by code (`PSS10_4`) while
this export's headers are question sentences. `from_plan` now resolves each
item to whichever name exists in the dataframe - without which the feature was
useless on precisely the dataset it was built for.

## The study plan did its job

Five `study_plan_tool` calls, fetched as slices, never the whole file:
`overview`, `variables`, `analysis`, `items(PSS10)`, `items(PSQ18)`.

The decisive part is the 13 reverse-coded items **read as fact**, including
nine PSQ-18 items. PSQ-18's reverse set is not inferable from column names the
way PSS-10's was in test-3 - and here the columns are not even named after the
instrument, they are raw question wording. Without the plan this file cannot be
scored correctly by any amount of inference.

## What it could not do, and said so

**The multivariable linear regression failed.** The plan's third planned test
did not run: statsmodels/patsy formula parsing failed, and after renaming
columns a `ValueError: zero-size array to reduction operation maximum` came
back from statsmodels' exog handling. The agent reported this plainly -
*"I report the failure plainly rather than invent results"* - listed the exact
error, and offered two concrete retries (`sm.OLS` on arrays instead of formula
parsing being the sensible one).

That is the correct behaviour and the hard rule in `SYSTEM_PROMPT` working: the
baseline_table and spearman stages completed with real numbers, the regression
did not, and the report says which is which. But **the run is incomplete against
its own plan** - one of three planned tests is missing.

**`PSQ18_OVERALL_MEAN` was not produced under that name.** The plan's declared
outcome does not exist in the CSV; `score_items_tool` created
`Patient_satisfaction_score` instead. Same 18 items, different name. The agent
flagged the discrepancy rather than silently substituting, which is right, but
the plan's variable naming is still unhonoured.

## Still open

- **The regression.** Fitting via `sm.OLS` on arrays rather than patsy formulas
  is the obvious next step; formula parsing on 58 sentence-length column names
  is a losing game.
- **Seven PSQI components are `method: "custom"`** with no algorithm anywhere in
  the JSON - the plan points at a `scoring.notes` that the file does not
  contain. The agent scored PSQI as a flat 14-item scale instead of the seven
  published components the plan actually specifies. It did not invent banding,
  but it also did not flag the substitution as prominently as it flagged the
  regression failure. Getting `scoring.notes` into the export is the fix.
- **The gate phase still cannot read the plan** (only `read_excel_tool`,
  `profile_tool`, `submit_plan_tool`), so it decides ambiguity without access
  to the declared outcome.
- **The data dictionary is ~2,521 tokens** here versus ~766 in test-3, because
  the headers are sentences. It duplicates much of what `column_map` serves on
  demand, but cannot simply be truncated - the agent needs exact names to index
  the dataframe.

## Comparison

| | test-3 | test-4 attempt 1 | test-4 (this) |
|---|---|---|---|
| dataset | 53 x 50, coded | 292 x 58, question-text headers | same |
| study plan | none | JSON, slices | JSON, slices |
| scale commits | 33 in one turn | 19 turns, one each | **42 in one call** |
| reverse-coded | 4, inferred | 13, read as fact | 13, read as fact |
| tokens | 428,064 | 755,079 | **615,179** |
| outcome | completed | **stopped at cap** | **completed** |
| verified | to 10 dp | n/a | **to 10 dp** |
