# Bayesian analysis

Use only when the user or study plan requests it and the sandbox confirms the
required dependency is available. Do not invent Bayes factors.

- State priors in outcome units and justify them.
- Report posterior estimates with 95% credible intervals.
- For directional questions, report posterior probability computed from draws.
- Check convergence (R-hat < 1.01), effective sample size, trace behavior, and
  posterior predictive fit.
- Distinguish credible intervals from confidence intervals.
- Bayesian evidence can support practical equivalence only when a meaningful
  equivalence region was specified.

If PyMC/ArviZ are unavailable, report that limitation rather than silently
substituting a frequentist statistic or guessed Bayesian quantity.
