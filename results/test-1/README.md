# Test 1 — statistical-agent vs. the ICU nurses stress/sleep dataset

**Date:** 2026-09-09
**File under test:** `test-sets/Perceived_stress_and_sleep_quality_among_intensive_care_nurses_in_a_tertiary_psychiatric_hospital_simulated_coded.csv` (53 rows x 50 cols)
**Same file that made cognitivePal_Stat's analyze engine select `CONSENT` as its outcome.**

## What was and was not run

**Not run: the full agent loop.** `read_excel_tool` calls
`_get_sandbox().upload_file(...)` on its very first invocation, so an E2B
sandbox is required before any tool can execute. `excel-analysis-agent-backend/.env`
contains only `OPENAI_API_KEY`; `E2B_API_KEY` is absent, and it is not in
the shell environment or any other `.env` in this workspace. Add it and
re-run `uv run run_file.py <path>` for the live agent trace.

**Run instead: the deterministic tool layer** (`tools.py`) directly.
`read_excel`, `profile`, `classify_columns`, `infer_scale`, `group_items`
and `score_items` are plain pandas with no LLM and no sandbox, so they
were exercised in-process. These are the signals the agent would be
handed as its starting point, so their defects propagate into any run.

## Files here

| File | Contents |
|---|---|
| `00_summary.json` | Everything below in machine-readable form, incl. the full item correlation matrix |
| `01_classify_columns.csv` | Per-column suggested type, confidence, dtype, n_unique, min/max, %missing |
| `02_infer_scale.csv` | Per-item inferred point count, reverse-coding flag, confidence, actual observed range |
| `03_pss10_scoring_comparison.csv` | Correct PSS-10 total vs. what `tools.py`'s reversal formula produces, per respondent |

## Findings

### 1. Zero Likert items detected — the entire survey pipeline is bypassed

```
classification_counts = {"continuous": 48, "categorical": 2}
likert_items_detected  = []
identifiers_detected   = []
```

`_classify_column` (tools.py:205) returns `continuous` for *any* numeric
dtype before it ever checks for a Likert pattern; the Likert branch below
it only matches **text** labels against `_LIKERT_SCALES`. This file is
numerically coded, so all 33 instrument items — the 10 PSS-10 items, 14
PSQI items, 9 coping items — plus `AGE`, `SEX` and `CONSENT` all land in
one undifferentiated `continuous` bucket.

FR-9.1 through FR-9.8 (classification, scale inference, grouping,
scoring, alpha) are therefore unreachable on any pre-coded dataset. The
agent can override the suggestion, but it must do so 33 times against a
"medium"-confidence wrong answer with no signal pointing at the mistake.

`RESPONDENT_ID` and `SUBMITTED_AT` are also missed — classed
`categorical` (low confidence), not `identifier`, because
`_IDENTIFIER_KEYWORDS` is only consulted on the non-numeric path *after*
the Likert check, and neither name matched.

### 2. Reverse-scoring is wrong for every 0-anchored instrument

`_score_series` (tools.py:294) reverses with `(n_points + 1) - x`. That is
the 1-based formula, correct for a 1-5 scale. PSS-10 is anchored at 0
(0 = never ... 4 = very often), where the correct reversal is `4 - x`,
i.e. `(n_points - 1) - x`.

Consequence, measured across all 53 records:

| | correct | tools.py formula |
|---|---|---|
| PSS-10 total, mean | 20.21 | 28.21 |
| PSS-10 total, range | 2–34 | 10–42 |

- **Every one of the 53 records is inflated by exactly +8** (2 points x 4 reverse-coded items). Zero exceptions.
- **3 records exceed the instrument's theoretical ceiling of 40** — an impossible score, and the only externally visible symptom.
- **29 of 53 records (55%) shift stress category** under the standard 0-13 / 14-26 / 27-40 bands: 20 moderate to high, 9 low to moderate.

This is the same class of defect as the analyze engine's `min >= 1`
ordinal rule: a hardcoded assumption that Likert scales start at 1.

### 3. `infer_scale` derives n_points from observed values, not the instrument

Every PSS item returned `n_points: 5, reverse_coded: False, confidence:
"low"`, with the note "Values are already numeric; assumed already on a
score scale in ascending order." `n_points` is `len(unique values
present)`, so a reverse-coded item where one response level happens to be
unused would silently get a different reversal constant than its
neighbours. The scale should come from the instrument definition, not
from what a 53-row sample happened to contain.

### 4. The grouping signal is genuinely good

`group_items` correctly separates the reverse-coded set on correlation
alone. `PSS10_4/5/7/8` correlate positively with each other (r = 0.34 to
0.45) and negatively with all six others (r = -0.15 to -0.35), while the
six direct items correlate positively among themselves (up to r = 0.63
for 9-10). The evidence the agent needs to flag reverse-coding is
present and clean — the tool surfaces it correctly and leaves the call to
the model, as designed.

### 5. Invalid sleep data is passed through silently

| Column | %missing | min | max |
|---|---|---|---|
| `PSQI_BEDTIME` | 100.0 | — | — |
| `PSQI_WAKETIME` | 100.0 | — | — |
| `PSQI_HOURS_SLEEP` | 0.0 | 18.0 | 61.0 |
| `PSQI_LATENCY_MIN` | 0.0 | 18.0 | 66.0 |

`profile` reports the missingness, so the two empty columns are at least
visible. But nothing flags `PSQI_HOURS_SLEEP` — 18 to 61 "hours" of sleep
per night is physically impossible, and it is exactly the variable whose
absence caused the other engine to discard its study design. There is no
range or unit check anywhere in the tool layer, so this reaches the model
as an ordinary continuous column.

`structural_issues` came back empty `{}` — that check covers blank rows,
merged cells and multi-row headers only, not value validity.

## Bearing on the migration plan

Findings 1, 2 and 5 map onto Phase 1 and Phase 2 items already scoped:

- **Finding 1** to instrument registry + codebook ingestion. A coded file
  carries no wording, so classification cannot be recovered from values
  alone — it has to come from the design.
- **Finding 2** to the same registry. Anchor (0- vs 1-based) must be a
  declared property of the instrument, and `_score_series` must take the
  reversal constant from it rather than from `n_points`. This is a
  one-line fix with a five-line test, and it is the highest-value item in
  the whole plan: silent, uniform, category-shifting corruption of the
  primary outcome.
- **Finding 5** to the pre-flight validity gate.

**Finding 4 is the encouraging one.** The correlation signal that would
catch a reverse-coding error is already correct and already surfaced. The
gap is that nothing downstream is obliged to act on it.

## Reproduce

```bash
cd statistical-agent/excel-analysis-agent-backend
uv sync
# full agent run (needs E2B_API_KEY in .env):
uv run run_file.py ../test-sets/Perceived_stress_and_sleep_quality_among_intensive_care_nurses_in_a_tertiary_psychiatric_hospital_simulated_coded.csv
```

The tool-layer probe that produced the files here is at
`/private/tmp/claude-501/-Users-pl-Desktop-cognitive-pal/cdba5691-dfe5-42e1-a4c6-a674bc3c6670/scratchpad/probe_tools.py`
(session scratchpad — copy it into the repo if you want it kept).
