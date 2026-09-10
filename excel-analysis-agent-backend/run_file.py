"""Run the agent against any file from the command line.

main.py is a fixed smoke test - one hardcoded file, one hardcoded
question. This does the same thing but takes both as arguments, so you
can point it at a new dataset without editing code.

    uv run run_file.py <path-to-csv-or-xlsx> ["your question"] [--plan plan.json]

--plan is optional. It points at a JSON analysis brief pinning down what
the agent would otherwise infer - any of:

    {
      "outcome":       "PSQI_5A",
      "predictors":    ["AGE", "SEX", "PSS10_1"],
      "analysis_plan": "score the subscales, then test stress vs sleep",
      "notes":         "PSS-10 is 0-4 anchored; items 4,5,7,8 reverse-scored"
    }

Every field is optional, and so is the file: give none of it and the agent
works everything out on its own, exactly as it does without --plan. Give
part of it and only that part is settled. Columns named under outcome and
predictors are checked against the file before the first LLM call, so a
typo costs nothing rather than producing a confident analysis of the wrong
column. See example_plan.json.

Defaults to assume_and_state=True, meaning the agent states its
assumptions and completes the run instead of stopping to ask a
clarifying question (see CLAUDE.md's note on the FR-2 gate phase). Pass
--ask to get the clarifying-question behaviour instead.
"""

import sys
from pathlib import Path

from agent import load_brief, run
from study_plan import is_study_plan

DEFAULT_QUESTION = (
    "Perform a complete analysis of this dataset. "
    "First, classify every column as identifier, continuous, categorical, or Likert. "
    "For each Likert item, infer its point scale and label-to-score map, and decide "
    "whether it is reverse-coded relative to the other items in its construct. "
    "Second, group the Likert items into their subscales, score each subscale, and "
    "report Cronbach's alpha for each with a judgement on whether it is acceptable. "
    "Third, choose and run the appropriate statistical tests to examine how the "
    "subscale scores relate to each other and to the demographic variables. "
    "State clearly which columns you could NOT analyse and why. "
    "Present the findings with the actual numbers."
)


def main() -> int:
    argv = sys.argv[1:]
    ask = "--ask" in argv

    # --plan <file.json>: an optional analysis brief (outcome / predictors /
    # analysis_plan / notes, each optional on its own). Omit it entirely and
    # the agent works everything out for itself, exactly as before.
    plan_path = None
    if "--plan" in argv:
        i = argv.index("--plan")
        if i + 1 >= len(argv):
            print("--plan needs a path to a JSON file")
            return 2
        plan_path = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]

    args = [a for a in argv if a != "--ask"]

    if not args:
        print(__doc__)
        return 2

    file_path = str(Path(args[0]).expanduser().resolve())
    if not Path(file_path).exists():
        print(f"No such file: {file_path}")
        return 2

    question = args[1] if len(args) > 1 else DEFAULT_QUESTION

    # --plan accepts either shape: the small outcome/predictors brief, or a
    # full study-plan JSON (recognised by "document": "study_plan"). They are
    # told apart by content, not by filename - one real study plan arrived
    # with a .csv extension and another study's name on it.
    brief = None
    study_plan_path = None
    if plan_path:
        try:
            import json as _json
            payload = _json.loads(Path(plan_path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"Could not read {plan_path}: {exc}")
            return 2
        if is_study_plan(payload):
            study_plan_path = plan_path
            print(f"Using study plan from {plan_path}: {payload.get('title', '')[:70]}")
        else:
            try:
                brief = load_brief(plan_path)
            except (ValueError, TypeError, OSError) as exc:
                print(f"Could not read the analysis brief: {exc}")
                return 2
            print(f"Using analysis brief from {plan_path}")

    try:
        result = run(
            file_path,
            question,
            assume_and_state=not ask,
            brief=brief,
            study_plan_path=study_plan_path,
        )
    except ValueError as exc:
        # validate_brief rejects a brief naming columns the file lacks -
        # deliberately before any LLM call, so this costs nothing.
        print(f"\n{exc}")
        return 2

    if result["status"] == "needs_clarification":
        print("\n=== Clarifying question ===")
        print(result["question"])
    elif result["status"] == "token_budget_exceeded":
        print("\n=== Stopped: token budget exceeded (MAX_RUN_TOKENS) ===")
        print(result["answer"])
    elif result["status"] == "step_limit_exceeded":
        print("\n=== Stopped: step limit exceeded (FR-7.3 guardrail) ===")
        print(result["answer"])
    else:
        print("\n=== Final answer ===")
        print(result["answer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
