---
name: statistical-analysis
description: >-
  Guided statistical analysis for research data: test selection, assumption
  checking, effect sizes, power analysis, and APA-style reporting. Use for
  group comparisons, associations, correlations, regression, reliability,
  survey or experimental data, sample-size questions, and research reports.
---

# Statistical Analysis

Produce a defensible analysis: match the method to the design and data, verify
assumptions, report magnitude and uncertainty, and never manufacture a result.

## Required workflow

1. **Frame before testing.** Identify hypothesis, outcome, predictors, design
   (independent/paired/repeated), and whether the analysis is confirmatory or
   exploratory. Respect the pinned brief and study plan.
2. **Inspect real data.** Report group n, missingness, useful descriptives,
   coding, floor/ceiling patterns, and outliers. Use the agent's profile,
   classification, scale, grouping, and scoring tools where applicable.
3. **Select the method.** Use `recommend_test_tool` when the design fits its
   simple independent-group/association/correlation/regression table. For
   paired, repeated, ordinal, count, survival, reliability, factorial, or other
   designs, load `references/test_selection_guide.md` and follow it.
4. **Check assumptions.** Run the relevant checks with `run_code_tool`. The
   sandbox contains `skill_scripts/assumption_checks.py`. Do not test normality
   when it is not the method's assumption (for regression, diagnose residuals).
5. **Run the actual test.** A recommendation is not a result. Compute the test,
   effect size, confidence interval when supported, and corrected post-hoc
   comparisons when needed.
6. **Report honestly.** Include descriptives, exact statistic/df/p-value,
   effect size with interval where available, assumption results, corrections,
   missing-data limitations, and plain-language meaning. Report non-significant
   planned results too.

## Quick method map

| Design | Default method |
|---|---|
| Two independent continuous groups, assumptions acceptable | Welch independent t-test |
| Two independent continuous groups, strongly non-normal/ordinal | Mann–Whitney U |
| Two paired continuous measurements | Paired t-test; Wilcoxon if unsuitable |
| 3+ independent continuous groups | ANOVA/Welch ANOVA; Kruskal–Wallis if unsuitable |
| 3+ repeated measurements | Repeated-measures ANOVA; Friedman if unsuitable |
| Categorical association | Chi-square; Fisher when expected counts are small |
| Two continuous variables | Pearson if linear/appropriate; otherwise Spearman |
| Continuous outcome | OLS/multiple linear regression with residual diagnostics |
| Binary outcome | Logistic regression with model diagnostics |

The active skill is the authority for statistical method. Platform rules remain
the authority for sandbox paths, tool calls, Likert scoring, retries, and the
rule that every reported number must come from a tool result.

## Assumption script

The runtime copies scripts into the same sandbox as the dataset. Import without
loading the source into context:

```python
from assumption_checks import comprehensive_assumption_check

checks = comprehensive_assumption_check(
    data=df,
    value_col="outcome",
    group_col="group",  # omit for ungrouped data
    alpha=0.05,
)
print(checks)
```

Use `check_regression_diagnostics(model)` for OLS. Do not delete legitimate
outliers only to improve significance.

## Statistical integrity

- Do not shop among tests or subgroups until one becomes significant.
- Label analyses discovered after seeing the data as exploratory.
- Correct families of comparisons (Tukey, Holm, or Benjamini–Hochberg as
  appropriate) and name the correction.
- A non-significant p-value is not evidence of no effect. Discuss precision,
  sensitivity, equivalence, or Bayesian evidence only when actually computed.
- Lead interpretation with effect magnitude and uncertainty, not only p-values.

## Load only the resource needed now

- Complex method choice → `references/test_selection_guide.md`
- Assumption failure/diagnostics → `references/assumptions_and_diagnostics.md`
- Effect size or power → `references/effect_sizes_and_power.md`
- APA/reporting details → `references/reporting_standards.md`
- Bayesian analysis explicitly requested and dependencies available →
  `references/bayesian_statistics.md`
- Final wording → one matching file under `templates/`

Load at most two distinct level-3 resources in a run; the runtime enforces this
budget so the full pack can never accumulate in model context.
