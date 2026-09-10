# Test 3 — first correct end-to-end run (Azure gpt-5-mini)

**Date:** 2026-09-10 · **File:** same ICU nurses stress/sleep CSV (53 x 50)
**Run:** 15 steps, 45 tool calls, 428,064 tokens, exit 0, **0 summarization events**
**Model:** `gpt-5-mini` via Azure (`LLM_SOURCE=azure`), seed=42, reasoning_effort=medium
`report.md`, `results.json`, `trace.log` copied verbatim from
`outputs/.../20260909_201128/`; `console.log` is the live run trace.

## Verdict

Every failure in test-2 is fixed. Every number the agent reported was
independently recomputed from the raw CSV and matches **to 10 decimal
places** — no fabrication anywhere in the report.

| | test-2 (gpt-4o-mini) | test-3 (gpt-5-mini) | truth |
|---|---|---|---|
| PSS-10 reverse-coded items | **none** | **PSS10_4, 5, 7, 8** | 4, 5, 7, 8 |
| PSS-10 Cronbach's alpha | 0.481, never reported | **0.832** | 0.832 |
| PSS by SEX | NaN, "insufficient sample size" | **U=166.5, p=0.0011, n=[25,28]** | U=166.5, p=0.0011 |
| PSS x sleep | r=0.136, p=0.330 → "null" | **rho=0.565, p=1.04e-05** | rho=0.565 |
| Coping subscales | 9 columns silently dropped | **3 subscales scored** | 0.760 / 0.754 / 0.668 |
| Single-item "subscales" | 4 (alpha undefined) | **0** | — |
| Likert items detected | 2/50 | **33** | — |
| Summarization events | 6 in 13 steps | **0** | — |

**The study's central hypothesis - stress relates to sleep quality - is
true in this data. test-2 reported it as null. test-3 finds it at
rho = 0.565, p = 1.04e-05.**

## What fixed it

Three changes, in order of impact.

**1. Compaction trigger 8,000 -> 100,000 tokens.** This was the root
cause of test-2's headline failure. At 8k the middleware fired 6 times in
13 steps, evicting the `profile_tool` output that held SEX's real values;
the agent then wrote `# Assuming SEX is coded as 0 and 1` against a column
coded `{1: 25, 2: 28}`, filtered an empty group, and reported the
resulting NaN as "insufficient sample size" - blaming the data for its own
guess. **Test-3 fired compaction zero times** and used the real groups.

**2. Pinned data dictionary.** Every column's actual values are now
rendered into phase 2's system prompt, which compaction cannot touch -
the same protection `handle_id`/`sandbox_path` already had after this
project was bitten the same way twice before. The line that matters:

```
- SEX [int64]: {'2': 28, '1': 25}
```

~766 tokens for all 50 columns.

**3. Model: gpt-4o-mini -> gpt-5-mini (a reasoning model).** Reverse-coding
went 0/4 -> 4/4 with zero false positives. Full gpt-5 scored the same 4/4
in an earlier aborted run, so the cheaper tier holds the accuracy.

Note that fixes 1 and 2 are independent of the model, and fix 3 is
independent of both: test-2's failures had two separate causes (context
eviction for SEX, model judgment for reverse-coding) and needed both.

## What the agent got right that test-2 missed entirely

- **Found all five constructs**, including the three coping subscales
  test-2 never classified, scored or mentioned. Their alphas (0.760,
  0.754, 0.668) match ground truth exactly.
- **Ran real normality checks first** and let them pick the test: PSS10
  failed Shapiro (W=0.950, p=0.026), so it used Spearman and Mann-Whitney
  rather than Pearson and t-test. test-2 asserted normality and used
  parametric tests regardless.
- **Reported what it could not analyse and why**: CONSENT (zero variance),
  PSQI_BEDTIME and PSQI_WAKETIME (no non-missing values). test-2's
  "Columns Not Analyzed" section omitted the nine columns it had dropped.
- **Disclosed n_incomplete_respondents = 3** for the PSQI group, so the
  reliability figure's smaller base is visible rather than buried.

## Known caveat: the 0-anchor scoring bug is still present

`tools.py:293` reverses an item as `(n_points + 1) - x`, which assumes a
1-anchored scale. PSS-10 is scored **0-4**, so `n_points=5` gives `6 - x`
where correct is `4 - x`. Every reverse-coded item is inflated by 2, and
the reported subscale mean by **+0.8**:

- reported `PSS10_total` mean: **2.821**
- correct mean: **2.021**

This is invisible to alpha, correlations and group tests - a constant
shift changes none of them, which is exactly why it has survived this
long, and why all of test-3's inferential statistics are still correct.
But **any reported mean subscale score is wrong**, and it would corrupt
comparison against published PSS-10 norms. This is the same bug test-1
flagged. Not fixed here: the fix needs a decision about how `infer_scale`
should represent a scale's anchor rather than just its point count.

## Cost

428,064 tokens (404,239 in / 23,825 out), against the new 750,000
`MAX_RUN_TOKENS` ceiling - 57% of budget, so the cap never engaged but
would have caught a runaway. For comparison, the aborted full-gpt-5 run
under the old 8k trigger reached 1,832,344 tokens in 83 turns without
producing an answer.

Tool-call profile shows no retry loop: `profile_tool` 2, `recall_memory_tool`
1, `classify_columns_tool` 2 - against 19/16/19 respectively in that
runaway. The 33 `infer_scale_tool` calls are one commit per Likert item.

## Still open

- The 0-anchor scoring bug above.
- Codebook ingestion. gpt-5-mini inferred the PSS-10 reverse items from
  column names alone, which worked here because `PSS10_4` is a recognisable
  instrument. A bespoke questionnaire with opaque column names would give
  it nothing to reason from.
- Reproducibility across repeat runs is unmeasured. `seed=42` is
  best-effort only; `temperature=0` is not available on gpt-5 models
  (HTTP 400 - only the default is supported), so run-to-run drift has to
  be measured rather than assumed away.
