"""
Smoke test: point the agent at one sample file and one question, and see
it work out the answer on its own by calling tools (instead of us
hardcoding read -> profile -> compute -> answer).
"""

from agent import run

FILE_PATH = "data/Perceived_Stress_and_Coping_Strategies_among_Nurses_in_Acute_simulated.csv"
QUESTION = (
    "I want you to perform a complete analysis of this dataset. "
    "First, classify every column as identifier, continuous, categorical, or Likert. "
    "For each Likert item, infer its point scale (points, label-to-score map) "
    "and decide whether it's reverse-coded relative to the other stress items. "
    "Second, perform a chi-square test to see if there is a significant "
    "association between 'Sex' and 'Have you been diagnosed with any long-term "
    "medical condition?'. "
    "Third, run a logistic regression to predict 'Have you been admitted to hospital "
    "for this condition before?' using 'Age', 'Sex', and 'Perceived Stress Score'. "
    "Present the findings clearly."
)

if __name__ == "__main__":
    answer = run(FILE_PATH, QUESTION)
    print("\n=== Final answer ===")
    print(answer)
