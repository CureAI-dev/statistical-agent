# Autonomous Excel Analysis Agent

An autonomous agent that takes an Excel/CSV file and a plain-English analysis request, and analyzes the data on its own (read → plan → run code → report). It is built specifically for survey/questionnaire data (Likert scales, scoring, chi-square, logistic regression), but works seamlessly on general spreadsheets too.

## Features
- **Data Profiling**: Automatically loads a file into a pandas dataframe and summarizes it (shape, dtypes, nulls, sample rows).
- **Secure Sandbox Execution**: Code runs in an isolated E2B cloud sandbox, ensuring no arbitrary code executes on the host machine.
- **Automated Column Classification**: Suggests types per column (likert, categorical, open_ended, identifier, continuous) using unique-value counts and matching against common Likert wordings.
- **Statistical Test Recommendation**: Automatically selects appropriate statistical tests (t-test, ANOVA, chi-square, correlation, regression) based on data normality checks.

## Project Structure
```
Autonomus Agent/
├── docs/requirements.md              # Full specification and source of truth
└── excel-analysis-agent-backend/     # The codebase (Python, uv-managed)
    ├── main.py                       # Smoke test script
    ├── agent.py                      # The LangChain ReAct agent loop
    ├── tools.py                      # Tools for file reading, profiling, test picker
    ├── sandbox_tool.py               # E2B sandbox wrapper
    └── data/                         # Sample CSVs for testing
```

## Getting Started

### Prerequisites
- Python (managed via `uv`)
- E2B API Key (for sandbox execution)
- OpenAI API Key (for the LLM, currently using `gpt-4o-mini`)

### Installation & Running

1. Navigate to the backend directory:
   ```bash
   cd excel-analysis-agent-backend
   ```

2. Create a `.env` file in the `excel-analysis-agent-backend` directory and add your API keys:
   ```env
   E2B_API_KEY=your_e2b_api_key
   OPENAI_API_KEY=your_openai_api_key
   ```

3. Run the smoke test using `uv`:
   ```bash
   uv run main.py
   ```

## Development Rules
1. **Simplicity First**: Do not add unnecessary abstractions or "just in case" flexibility. If a plain function does the job, use it.
2. **Security**: All data-processing code must go through the E2B sandbox. Never run arbitrary code locally using `exec`.
3. **Data Immutability**: The source spreadsheet is never modified. The agent works on a copy inside the sandbox.
4. **Package Management**: The project strictly uses `uv` (`pyproject.toml`, `uv.lock`). Do not switch to pip or poetry.
