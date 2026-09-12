"""Independent, read-only reproduction of test-4 findings; no LLM calls.

Run with the backend virtualenv's Python. Writes reviewer_verification.json only.
The reduced OLS fit is a software diagnostic, not the planned PSQI model.
"""
from pathlib import Path
import json
import re
import hashlib
import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm
import statsmodels.formula.api as smf

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / 'test-sets' / 'test-set-4'
plan_path = next(DATA.glob('Sleep*'))
csv_path = next(DATA.glob('Perceived*'))
plan = json.loads(plan_path.read_text())
raw = pd.read_csv(csv_path)
saved = json.loads((HERE / 'results.json').read_text())
questions = [q for s in plan['questionnaire']['sections'] for q in s['questions']]
correct, legacy = {}, {}
for q in questions:
    if q['response_type'] != 'likert':
        continue
    mapping = {o['label']: o['value'] for o in q['options']}
    x = raw[q['text']].map(mapping).astype(float)
    anchors = list(mapping.values())
    correct[q['column']] = min(anchors) + max(anchors) - x if q['reverse_coded'] else x
    legacy[q['column']] = len(mapping) + 1 - x if q['reverse_coded'] else x
correct, legacy = pd.DataFrame(correct), pd.DataFrame(legacy)

def summary(x):
    return {'n': int(x.count()), 'mean': float(x.mean()), 'sd': float(x.std()),
            'min': float(x.min()), 'max': float(x.max())}

def alpha(x):
    x = x.dropna()
    k = x.shape[1]
    return float(k / (k - 1) * (1 - x.var().sum() / x.sum(axis=1).var()))

score = pd.DataFrame(index=raw.index)
score['Patient_satisfaction_score'] = correct.filter(regex='^PSQ18_').mean(axis=1)
score['PSS10_score'] = legacy.filter(regex='^PSS10_').mean(axis=1)
score['PSQI_score'] = correct.filter(regex='^PSQI_').mean(axis=1)
out = {'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [csv_path, plan_path]},
       'shape': list(raw.shape), 'correct_pss_total': summary(correct.filter(regex='^PSS10_').sum(axis=1)),
       'correct_pss_mean': summary(correct.filter(regex='^PSS10_').mean(axis=1)),
       'legacy_pss_mean': summary(score.PSS10_score),
       'scores': {c: summary(score[c]) for c in score},
       'alphas': {p: alpha(correct.filter(regex='^' + p + '_')) for p in ['PSQ18', 'PSS10', 'PSQI']},
       'psqi_alpha_n': len(correct.filter(regex='^PSQI_').dropna())}
out['correlations'] = {}
for a, b in [('Patient_satisfaction_score', 'PSS10_score'), ('Patient_satisfaction_score', 'PSQI_score'), ('PSS10_score', 'PSQI_score')]:
    r = stats.spearmanr(score[a], score[b])
    out['correlations'][a + '_vs_' + b] = {'rho': float(r.statistic), 'p': float(r.pvalue)}
demo = raw.rename(columns={q['text']: q['column'] for q in questions})
out['group_tests'] = {}
for col, first, second in [('SEX', 'Male', 'Female'), ('COMORBIDITY', 'Yes', 'No')]:
    x, y = [score.loc[demo[col].eq(v), 'Patient_satisfaction_score'] for v in [first, second]]
    t = stats.mannwhitneyu(x, y, alternative='two-sided')
    out['group_tests'][col] = {'U': float(t.statistic), 'p': float(t.pvalue),
        first: summary(x), second: summary(y)}
t = stats.kruskal(*[score.loc[demo.EDUCATION.eq(v), 'Patient_satisfaction_score'] for v in demo.EDUCATION.unique()])
out['group_tests']['EDUCATION'] = {'H': float(t.statistic), 'p': float(t.pvalue)}
out['sleep_hours'] = summary(demo.PSQI_HOURS_SLEEP)
out['sleep_hours']['over_24'] = int(demo.PSQI_HOURS_SLEEP.gt(24).sum())
out['missing_by_code'] = {c: int(n) for c, n in demo.isna().sum().items() if n}
out['subscales'] = {}
for v in plan['variables']['all']:
    if v['method'] in ['sum', 'mean'] and set(v['source_items']).issubset(correct.columns):
        x = correct[v['source_items']]
        value = x.sum(axis=1) if v['method'] == 'sum' else x.mean(axis=1)
        out['subscales'][v['column']] = {**summary(value), 'alpha': alpha(x)}

# Reproduce the final attempt's unintended inclusion of every numeric column.
bad = raw.join(score).rename(columns={q['text']: q['column'] for q in questions if q['column'] in ['AGE', 'SEX', 'EDUCATION', 'COMORBIDITY']})
bad = bad.rename(columns={'Patient_satisfaction_score': 'psq_score', 'PSS10_score': 'pss_score', 'PSQI_score': 'psqi_score', 'AGE': 'age', 'SEX': 'sex', 'EDUCATION': 'education', 'COMORBIDITY': 'comorbidity'})
bad = pd.get_dummies(bad, columns=['sex', 'education', 'comorbidity'], drop_first=True)
bad.columns = [re.sub(r'\W+', '_', str(c)) for c in bad.columns]
numeric = [c for c in bad if pd.api.types.is_numeric_dtype(bad[c]) and c != 'psq_score']
out['regression_failure'] = {'numeric_predictors': numeric,
    'all_missing_predictors': [c for c in numeric if bad[c].isna().all()],
    'complete_rows': len(bad[['psq_score'] + numeric].dropna())}
try:
    smf.ols('psq_score ~ ' + ' + '.join(numeric), data=bad, missing='drop').fit()
except Exception as e:
    out['regression_failure']['reproduced_error'] = type(e).__name__ + ': ' + str(e)

# Diagnostic only: the intended reduced model in attempt 1 is computable.
md = score.join(demo[['AGE', 'SEX', 'EDUCATION', 'COMORBIDITY']]).dropna()
x = pd.get_dummies(md.drop(columns='Patient_satisfaction_score'), drop_first=True).astype(float)
x = sm.add_constant(x)
fit = sm.OLS(md.Patient_satisfaction_score.astype(float), x).fit()
out['reduced_model_diagnostic_only'] = {'n': int(fit.nobs), 'r_squared': float(fit.rsquared),
    'matrix_rank': int(np.linalg.matrix_rank(x)), 'matrix_columns': x.shape[1],
    'warning': 'Uses the nonstandard PSQI proxy and legacy PSS mean; not the planned analysis.'}

text = (HERE / 'trace.log').read_text()
out['turns'] = []
for i, block in enumerate(text.split('================================== Ai Message ==================================')[1:], 1):
    token = re.search(r'tokens this call: (\d+) in / (\d+) out', block)
    name = re.search(r'  (\w+_tool) \(', block)
    out['turns'].append({'turn': i, 'tool': name.group(1) if name else 'final',
        'input': int(token[1]), 'output': int(token[2]), 'total': int(token[1]) + int(token[2])})
out['timed_tool_seconds'] = sum(t['seconds'] for t in saved['tool_calls'])
assert sum(t['total'] for t in out['turns']) == saved['token_usage']['total']
assert np.allclose(score.PSS10_score - correct.filter(regex='^PSS10_').mean(axis=1), 0.8)
(HERE / 'reviewer_verification.json').write_text(json.dumps(out, indent=2) + '\n')
print(json.dumps({k:v for k,v in out.items() if k not in ['source_sha256','turns','subscales']}, indent=2))
