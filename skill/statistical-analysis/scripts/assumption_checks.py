"""Statistical diagnostics used by the bundled statistical-analysis skill.

No plotting is performed so output remains compact for the agent context.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def _clean_numeric(values) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)


def check_normality(values, name: str = "outcome", alpha: float = 0.05) -> dict:
    data = _clean_numeric(values)
    if len(data) < 3:
        return {"test": "Shapiro-Wilk", "n": len(data), "ok": None, "reason": "fewer than 3 observations"}
    statistic, p_value = stats.shapiro(data)
    return {
        "test": "Shapiro-Wilk",
        "variable": name,
        "n": len(data),
        "statistic": float(statistic),
        "p_value": float(p_value),
        "ok": bool(p_value > alpha),
    }


def check_normality_per_group(
    data: pd.DataFrame,
    value_col: str,
    group_col: str,
    alpha: float = 0.05,
) -> list[dict]:
    rows = []
    for group, frame in data.groupby(group_col, dropna=False):
        rows.append({"group": str(group), **check_normality(frame[value_col], value_col, alpha)})
    return rows


def check_homogeneity_of_variance(
    data: pd.DataFrame,
    value_col: str,
    group_col: str,
    alpha: float = 0.05,
) -> dict:
    groups = [_clean_numeric(frame[value_col]) for _, frame in data.groupby(group_col)]
    groups = [group for group in groups if len(group) >= 2]
    if len(groups) < 2:
        return {"test": "Levene", "ok": None, "reason": "fewer than 2 usable groups"}
    statistic, p_value = stats.levene(*groups, center="median")
    variances = [float(np.var(group, ddof=1)) for group in groups]
    nonzero = [value for value in variances if value > 0]
    ratio = max(variances) / min(nonzero) if nonzero else None
    return {
        "test": "Levene",
        "statistic": float(statistic),
        "p_value": float(p_value),
        "ok": bool(p_value > alpha),
        "variance_ratio": ratio,
    }


def detect_outliers(values, threshold: float = 1.5) -> dict:
    data = _clean_numeric(values)
    if not len(data):
        return {"method": "IQR", "n": 0, "n_outliers": 0, "indices": []}
    q1, q3 = np.percentile(data, [25, 75])
    iqr = q3 - q1
    lower, upper = q1 - threshold * iqr, q3 + threshold * iqr
    indices = np.where((data < lower) | (data > upper))[0]
    return {
        "method": "IQR",
        "n": len(data),
        "n_outliers": int(len(indices)),
        "pct_outliers": float(100 * len(indices) / len(data)),
        "indices": indices.tolist(),
        "lower_bound": float(lower),
        "upper_bound": float(upper),
    }


def check_regression_diagnostics(model, alpha: float = 0.05) -> dict:
    from statsmodels.stats.diagnostic import het_breuschpagan
    from statsmodels.stats.outliers_influence import variance_inflation_factor
    from statsmodels.stats.stattools import durbin_watson

    residuals = np.asarray(model.resid)
    exog = np.asarray(model.model.exog)
    names = list(model.model.exog_names)
    sw_stat, sw_p = stats.shapiro(residuals)
    bp_stat, bp_p, _, _ = het_breuschpagan(residuals, exog)
    vif = {
        name: float(variance_inflation_factor(exog, index))
        for index, name in enumerate(names)
        if name.lower() not in {"const", "intercept"}
    }
    return {
        "residual_normality": {
            "test": "Shapiro-Wilk",
            "statistic": float(sw_stat),
            "p_value": float(sw_p),
            "ok": bool(sw_p > alpha),
        },
        "heteroscedasticity": {
            "test": "Breusch-Pagan",
            "statistic": float(bp_stat),
            "p_value": float(bp_p),
            "ok": bool(bp_p > alpha),
        },
        "durbin_watson": float(durbin_watson(residuals)),
        "vif": vif,
    }


def comprehensive_assumption_check(
    data: pd.DataFrame,
    value_col: str,
    group_col: str | None = None,
    alpha: float = 0.05,
) -> dict:
    result = {
        "outliers": detect_outliers(data[value_col]),
        "missing_outcome": int(data[value_col].isna().sum()),
    }
    if group_col:
        result["normality_per_group"] = check_normality_per_group(data, value_col, group_col, alpha)
        result["homogeneity"] = check_homogeneity_of_variance(data, value_col, group_col, alpha)
        result["group_sizes"] = {
            str(key): int(value)
            for key, value in data.groupby(group_col, dropna=False)[value_col].count().items()
        }
    else:
        result["normality"] = check_normality(data[value_col], value_col, alpha)
    return result
