# Test 2 — first full agent run (local runtime, no E2B)

**Date:** 2026-09-09 · **File:** same ICU nurses stress/sleep CSV (53 x 50)
**Run:** 13 steps, 116,973 tokens, exit 0, zero infrastructure errors.
`report.md`, `results.json`, `trace.log` are copied verbatim from
`outputs/.../20260908_202959/`.

## What the agent reported

1. PSS scores normally distributed (Shapiro-Wilk W=0.987, p=0.827).
2. Pearson PSS vs PSQI: **r = 0.136, p = 0.330** — "weak positive
   correlation that is not statistically significant."
3. Independent t-test of PSS by SEX: **NaN** — "could not be performed
   due to insufficient sample size in one or both groups."

## What is actually true

| | agent | correct |
|---|---|---|
| PSS-10 Cronbach's alpha | **0.481** (unacceptable) | **0.832** (good) |
| PSS vs PSQI disturbance | r = 0.136, p = 0.330 (null) | **r = 0.542, p < 0.001** |
| PSS total by sex | NaN, "insufficient data" | **t = -3.663, p = 0.0006** |

Both headline findings are wrong, and both real effects are significant.
The study's central hypothesis — stress relates to sleep quality — is
**true in this data and the agent reported it as null.**

## Why

**1. Reverse-coding was never applied.** All 24 committed scales carry
`reverse_coded: False`. `PSS10_4/5/7/8` measure coping ability, so leaving
them un-reversed subtracts signal from the total. That single omission
drops alpha 0.832 -> 0.481 and collapses r 0.542 -> 0.136.

The agent had the evidence and discarded it. `score_items_tool` returned:

```
"cronbachs_alpha": 0.481,
"likely_reverse_coded_items": ["PSS10_5", "PSS10_7", "PSS10_8"]
```

Three of the four correct items, named explicitly, alongside an alpha
below every acceptability threshold. The agent did not loop back, and
never mentioned alpha in its report at all.

**2. The t-test used a coding the file does not have.** The agent wrote:

```python
# Assuming SEX is coded as 0 and 1 for two groups
male_scores   = df[df['SEX'] == 0][...]
female_scores = df[df['SEX'] == 1][...]
```

`SEX` is coded `{1: 25, 2: 28}`. `SEX == 0` matches zero rows. scipy
returned NaN with a SmallSampleWarning, and the agent reported that as
"insufficient sample size in one or both groups" — blaming the data for
its own wrong assumption, which it had flagged as an assumption in its
own comment one line earlier.

**3. Nine columns were silently dropped.** The three coping subscales
were never classified, scored or mentioned. They score fine:
ProblemFocused alpha 0.76, EmotionRegulation 0.754, SupportSeeking 0.668.
The report's "Columns Not Analyzed" section does not list them.

**4. Grouping was wrong.** Six groups: two 10-item groups plus four
single-item groups (`PSQI_6/7/8/9`), one of them named `PSQI Sleep
Quality` while another is `Pittsburgh Sleep Quality Index (PSQI) Sleep
Quality`. Single-item groups cannot have an alpha, hence four nulls.
PSQI_6-9 are separate PSQI components, not subscales.

**5. Classification was 2/50.** `{continuous: 48, categorical: 2}` — zero
Likert items detected, as test-1 predicted for a numerically coded file.

## The through-line

Every failure is the same failure: **the file carries codes, not wording,
and the agent has no access to the codebook.** It cannot know that
`PSS10_4` is "confidence in handling personal problems" and must be
reversed, that `SEX` is 1/2, that PSQI_6-9 are components rather than a
subscale, or that the coping blocks are three distinct constructs. All of
it is in the design's `project.json`, which the agent was never given.

The `likely_reverse_coded_items` hint proves the statistical machinery
works. What is missing is (a) the codebook, and (b) any obligation to act
on a failing alpha before reporting.

## Bearing on the plan

- **Phase 1 codebook ingestion** is now the critical path, not an
  optimisation. Without it this file cannot be analysed correctly by any
  model.
- **Phase 2's evidence gate** would have caught all three: alpha 0.481
  reported as nothing, an empty group reported as "insufficient data",
  nine columns dropped without disclosure.
- The instrument registry fixes reverse-coding, the 0-anchor scoring bug
  from test-1, and the PSQI component structure in one stroke.
