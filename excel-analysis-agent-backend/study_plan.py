"""Reading a study-plan JSON: the design document that says what a dataset
means and what analysis was planned for it.

Why this exists
---------------
Everything the agent used to *infer* about a survey - which columns are
Likert items, their anchors, which are reverse-scored, which belong to the
same subscale, what the outcome is - is stated as fact in this file. Test-3
showed the agent inferring all of it correctly, but only because the
columns were named after a famous instrument (`PSS10_4`) it could
recognise. A bespoke questionnaire gives it nothing to recognise. The plan
removes the guessing entirely.

For the paired telemedicine dataset it does something the agent cannot do
at all without it: the CSV's headers are raw question wording
("What is your age in completed years?"), while every variable the plan
analyses is named by code (AGE, PSS10_TOTAL). The plan is the only join
between the two, and 56 of that file's 58 columns resolve through it.

Why it is a tool and not pinned context
---------------------------------------
The whole file is ~11,300 tokens. Pinned into the system prompt it would be
resent on every call - about 170,000 tokens over a 15-call run, roughly 40%
of a whole run's input, to keep item wording in view during steps that have
nothing to do with items. The parts that are always needed (overview,
outcome/predictors, planned tests) come to ~750 tokens and *are* pinned; the
bulk is fetched a slice at a time. One item's spec is ~135 tokens.
"""

import json
from pathlib import Path
from typing import Any

# A study plan is recognised by these, not by filename - one real file
# arrived as ".csv" holding JSON, and named after a different study than
# the one it describes.
_MARKER_KEYS = ("document", "schema_version")


def is_study_plan(obj: Any) -> bool:
    """True for a study-plan document, as opposed to the small
    outcome/predictors/analysis_plan brief that --plan also accepts."""
    return isinstance(obj, dict) and obj.get("document") == "study_plan"


def load_study_plan(path: str) -> dict:
    """Read and lightly validate a study-plan JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: a study plan must be a JSON object, got {type(data).__name__}.")
    if not is_study_plan(data):
        raise ValueError(
            f"{path}: not a study plan - expected \"document\": \"study_plan\" "
            f"(found {data.get('document')!r}). Present keys: {sorted(data)[:8]}."
        )
    return data


def _questions(plan: dict) -> list[dict]:
    return [q for s in plan.get("questionnaire", {}).get("sections", []) for q in s.get("questions", [])]


def overview(plan: dict) -> dict:
    """The always-relevant header: what the study asks and of whom. Small
    enough to pin (~420 tokens)."""
    study = plan.get("study", {})
    return {
        "title": plan.get("title"),
        "design": study.get("design"),
        "population": study.get("population"),
        "n_analysed": study.get("n_analysed"),
        "research_question": plan.get("research_question"),
        "aim": plan.get("aim"),
        "primary_hypothesis": plan.get("primary_hypothesis"),
        "secondary_objectives": plan.get("secondary_objectives"),
    }


def planned_analysis(plan: dict) -> dict:
    """The tests the study set out to run, and why. This is a plan, not a
    licence to skip checking: a test named here still has to have its
    assumptions verified against the real data before it is trusted."""
    return plan.get("analysis", {})


def target_variables(plan: dict) -> dict:
    """Outcome and predictors, by name only - the derivation of each comes
    from variable_spec()."""
    variables = plan.get("variables", {})
    return {"outcome": variables.get("outcome"), "predictors": variables.get("predictors", [])}


def declared_columns(plan: dict) -> set[str]:
    """Every column name the plan knows: raw items plus derived variables.

    Used to check an analysis brief. A derived variable (PSS10_TOTAL) is a
    legitimate thing to name as an outcome even though it does not exist in
    the CSV yet - it does not exist until it is computed, which is the whole
    point of the plan describing how."""
    names = {q.get("column") for q in _questions(plan)}
    names |= {v.get("column") for v in plan.get("variables", {}).get("all", [])}
    return {n for n in names if n}


def column_map(plan: dict) -> dict[str, dict]:
    """Question wording -> coded column, subscale, reverse flag.

    The join between a raw survey export and the plan's variable names.
    Trimmed deliberately: full option lists belong to item_spec()."""
    return {
        (q.get("text") or "").strip(): {
            "column": q.get("column"),
            "subscale": q.get("subscale"),
            "reverse_coded": q.get("reverse_coded"),
        }
        for q in _questions(plan)
        if q.get("text") and q.get("column")
    }


def item_spec(plan: dict, column: str) -> dict | None:
    """One questionnaire item, stated rather than inferred: its wording,
    its subscale, whether it is reverse-scored, and its exact label->value
    anchors. This is what replaces guessing a scale from observed values -
    including the anchor, which is where the 0-vs-1 scoring bug comes from."""
    for q in _questions(plan):
        if q.get("column") == column:
            options = q.get("options") or []
            return {
                "column": column,
                "text": q.get("text"),
                "subscale": q.get("subscale"),
                "reverse_coded": q.get("reverse_coded"),
                "response_type": q.get("response_type"),
                "label_to_score": {o["label"]: o["value"] for o in options if "label" in o},
                "anchor": ([min(o["value"] for o in options), max(o["value"] for o in options)] if options else None),
            }
    return None


def variable_spec(plan: dict, column: str) -> dict | None:
    """A derived variable's definition: how it is built and from what.

    `method` is the executable part - "mean"/"sum" over source_items is
    unambiguous, while "custom" means the plan is deferring to a published
    algorithm it does not itself carry (the PSQI components do this). Treat
    "custom" as a flag to stop and say so, not as licence to invent one."""
    for v in plan.get("variables", {}).get("all", []):
        if v.get("column") == column:
            return {
                "column": column,
                "meaning": v.get("meaning"),
                "role": v.get("role"),
                "type": v.get("type"),
                "method": v.get("method"),
                "source_items": v.get("source_items"),
                "range": v.get("range"),
                "instrument": v.get("instrument"),
                "derivation": v.get("derivation"),
            }
    return None


def items_for(plan: dict, instrument: str | None = None, subscale: str | None = None) -> list[dict]:
    """Every item spec for one instrument or subscale - the batch form of
    item_spec, so scoring a whole scale costs one call rather than ten."""
    out = []
    by_instrument = {
        v.get("column"): v.get("instrument") for v in plan.get("variables", {}).get("all", [])
    }
    for q in _questions(plan):
        column = q.get("column")
        if subscale and q.get("subscale") != subscale:
            continue
        if instrument:
            section_matches = str(column or "").upper().startswith(instrument.upper().replace("-", ""))
            if not (section_matches or by_instrument.get(column) == instrument):
                continue
        spec = item_spec(plan, column)
        if spec:
            out.append(spec)
    return out


def reverse_coded_items(plan: dict) -> list[str]:
    """Every item the plan marks reverse-scored - stated, not judged."""
    return [q["column"] for q in _questions(plan) if q.get("reverse_coded") and q.get("column")]
