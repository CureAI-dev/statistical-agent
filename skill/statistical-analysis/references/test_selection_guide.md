# Test selection guide

Load this only when the quick map in `SKILL.md` does not settle the design.

## Group comparisons

- Two independent groups: Welch t-test for continuous outcomes when suitable;
  Mann–Whitney U for ordinal or strongly unsuitable distributions.
- Two paired measurements: paired t-test or Wilcoxon signed-rank.
- 3+ independent groups: one-way ANOVA, Welch ANOVA for unequal variances, or
  Kruskal–Wallis. Significant omnibus tests need corrected post-hoc analysis.
- 3+ repeated measurements: repeated-measures ANOVA or Friedman.
- Multiple factors: factorial ANOVA; covariates: ANCOVA; mixed within/between:
  mixed ANOVA or a mixed-effects model.
- Paired binary outcomes: McNemar; 3+ paired binary occasions: Cochran's Q.

## Relationships and outcomes

- Continuous pair: Pearson for an approximately linear relationship without
  influential anomalies; Spearman/Kendall for monotonic ordinal/non-normal data.
- Continuous outcome: OLS/multiple regression; diagnose residuals,
  heteroscedasticity, influence, and collinearity.
- Binary outcome: logistic regression. Rare/separated events may need Firth or
  exact methods.
- Count outcome: Poisson; negative binomial when overdispersed; consider
  zero-inflated models only when structurally justified.
- Time-to-event: log-rank for curves; Cox regression for covariates, with
  proportional-hazards checking.

## Reliability and agreement

- Internal consistency: Cronbach's alpha and preferably omega; inspect
  item-total correlations rather than chasing alpha by arbitrary deletion.
- Categorical raters: Cohen's kappa (2), Fleiss/Krippendorff (>2).
- Continuous raters: ICC. Two measurement methods: Bland–Altman.

## Categorical tables

- Chi-square when expected counts are adequate.
- Fisher's exact for sparse 2x2 tables.
- Ordered categories: trend test where the ordering is substantive.
- Correct multiple related tests with Holm or FDR.
