from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox

load_dotenv()


class Runtime:
    # E2B's default sandbox timeout is short (a few minutes) and is meant
    # for one-off snippets, not a whole multi-step agent run sharing one
    # sandbox (see agent.py) - a file with more Likert items means more
    # tool calls and easily outlives that default, killing the sandbox
    # mid-run with a TimeoutException. 20 minutes covers any run so far.
    DEFAULT_TIMEOUT_SECONDS = 1200

    def __init__(self):
        self.sandbox = Sandbox.create(timeout=self.DEFAULT_TIMEOUT_SECONDS)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as file:
            self.sandbox.files.write(remote_path, file)

    def run_code(self, code: str) -> dict:
        execution = self.sandbox.run_code(code)
        return {
            "stdout": "".join(execution.logs.stdout),
            "stderr": "".join(execution.logs.stderr),
            # Jupyter-style auto-display of the last line's value (e.g. a
            # bare `df.shape` with no print()) - without this, code that
            # doesn't explicitly print() its answer looks like it produced
            # nothing.
            "result": execution.text,
            "error": str(execution.error) if execution.error else None,
        }

    def close(self) -> None:
        self.sandbox.kill()
