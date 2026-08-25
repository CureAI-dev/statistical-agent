"""
Smoke test: point the agent at one sample file and one question, and see
it work out the answer on its own by calling tools (instead of us
hardcoding read -> profile -> compute -> answer).
"""

from agent import run

FILE_PATH = "data/Perceived_Stress_and_Coping_Strategies_among_Nurses_in_Acute_simulated.csv"
QUESTION = (
    "This is a survey. Classify every column and tell me which ones are "
    "Likert-scale questionnaire items."
)

if __name__ == "__main__":
    answer = run(FILE_PATH, QUESTION)
    print("\n=== Final answer ===")
    print(answer)
