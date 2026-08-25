from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox

load_dotenv()


class Runtime:
    def __init__(self):
        self.sandbox = Sandbox.create()

#
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
