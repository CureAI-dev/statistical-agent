# Assumptions and diagnostics

Formal tests are evidence, not automatic switches. With large samples they flag
minor departures; inspect magnitude and plots when available.

## Group comparisons

- Independence comes from study design and cannot be repaired statistically.
- Check outcome distributions within groups, sample sizes, missingness,
  influential observations, and Levene's variance test.
- Unequal variance: prefer Welch t-test/Welch ANOVA.
- Strong skew or ordinal outcomes with small samples: use a justified
  rank-based or robust method.

## Regression

Diagnose the fitted model, not raw outcome normality:

- linearity / correct functional form
- residual heteroscedasticity (Breusch–Pagan; use HC3 robust SE where needed)
- residual distribution when small-sample inference depends on it
- independence/autocorrelation when observations are ordered
- multicollinearity (VIF)
- influential observations

## Outliers

Investigate data-entry errors and sensitivity. Never remove a legitimate value
solely to produce significance. Report the rule and whether conclusions change.

## Missing data

Report missingness by variable and group. Complete-case analysis is only
unbiased under suitable missingness assumptions; do not silently drop records.
