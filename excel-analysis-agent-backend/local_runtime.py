"""A drop-in replacement for sandbox_tool.Runtime that runs code on this
machine instead of in an E2B cloud sandbox, for when no E2B_API_KEY is
available.

This deliberately relaxes CLAUDE.md's rule 2 ("never run arbitrary code on
the host"), so it is not the default - agent_tools only reaches for it
when E2B is unavailable, and prints a warning when it does. The risk it
trades away is real: code the model writes can read and write anything
this user account can.

What it does keep:

- **A separate process.** Code runs in a child `python` process, not via
  `exec()` inside the agent. A crash, a `sys.exit()`, or a runaway memory
  allocation takes down the child, not the run.
- **A timeout.** A child that hangs is killed, where an in-process `exec`
  would hang the agent forever.
- **A working directory.** Uploaded copies of the data live in a temp dir
  that is deleted on close, so the original spreadsheet is never touched
  (CLAUDE.md rule 3).
- **Persistent state between calls.** The child holds one namespace for
  the whole run, so variables defined in one run_code call are still there
  in the next - the same behaviour E2B gives us (FR-9.7), which the agent
  relies on to build an analysis step by step.

The child is a small read-eval-reply loop speaking JSON over stdin/stdout,
one line per message, so nothing in the executed code's own stdout can be
confused for a protocol message (that output is captured separately).
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The worker's read-eval-reply loop. Kept as a string rather than a
# separate file so there is one thing to read and nothing to keep in sync.
_WORKER = r'''
import ast, base64, io, json, sys, traceback
from contextlib import redirect_stderr, redirect_stdout

ns = {"__name__": "__main__"}

def execute(code):
    """Run `code`, capturing stdout/stderr, and return the value of a bare
    trailing expression the way a Jupyter cell would - `df.shape` on the
    last line shows its value without an explicit print()."""
    out, err, text = io.StringIO(), io.StringIO(), None
    try:
        parsed = ast.parse(code)
    except SyntaxError:
        return {"stdout": "", "stderr": "", "result": None,
                "error": traceback.format_exc(limit=0).strip()}

    last_expr = None
    if parsed.body and isinstance(parsed.body[-1], ast.Expr):
        last_expr = ast.Expression(parsed.body.pop().value)

    error = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            if parsed.body:
                exec(compile(parsed, "<agent>", "exec"), ns)
            if last_expr is not None:
                value = eval(compile(last_expr, "<agent>", "eval"), ns)
                if value is not None:
                    text = repr(value)
    except BaseException:
        # BaseException, not Exception: a SystemExit or KeyboardInterrupt
        # raised by the executed code should be reported back as an error,
        # not silently end the worker mid-run.
        error = traceback.format_exc()

    return {"stdout": out.getvalue(), "stderr": err.getvalue(),
            "result": text, "error": error}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    reply = execute(base64.b64decode(msg["code"]).decode("utf-8"))
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
'''


class LocalRuntime:
    """Same three methods agent_tools calls on the E2B Runtime:
    upload_file, run_code, close - plus sandbox_path_for, so the caller
    doesn't have to know whether paths look like /home/user/x.csv (E2B) or
    a temp dir on this machine (here)."""

    DEFAULT_TIMEOUT_SECONDS = 1200

    def __init__(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="statagent-local-"))
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            text=True,
        )

    def sandbox_path_for(self, filename: str) -> str:
        """Where an uploaded file will live. E2B can write to /home/user;
        this machine can't, so uploads go to the run's temp workdir and the
        agent is told that path instead."""
        return str(self.workdir / filename)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        target = Path(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)

    def run_code(self, code: str) -> dict:
        # `%pip install x` is IPython syntax, which E2B's kernel understands
        # and a plain python process does not. Translate it rather than
        # failing: the agent (and agent_tools' own statsmodels install)
        # writes it either way.
        stripped = code.strip()
        if stripped.startswith(("%pip", "!pip")):
            return self._pip(stripped.split()[1:])

        if self._proc.poll() is not None:
            return {"stdout": "", "stderr": "", "result": None,
                    "error": "Local runtime process is no longer running."}

        import base64
        payload = json.dumps({"code": base64.b64encode(code.encode("utf-8")).decode("ascii")})
        try:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
            reply = self._proc.stdout.readline()
        except (BrokenPipeError, ValueError) as exc:
            return {"stdout": "", "stderr": "", "result": None,
                    "error": f"Local runtime died while running code: {exc}"}

        if not reply:
            return {"stdout": "", "stderr": "", "result": None,
                    "error": "Local runtime produced no reply (worker exited)."}
        return json.loads(reply)

    def _pip(self, args: list[str]) -> dict:
        """Install into the same interpreter the worker is running, so an
        import in the next run_code call actually finds the package.

        Already-satisfied installs short-circuit: the packages E2B ships by
        default (scipy) plus statsmodels are ordinary project dependencies
        here, so the usual case is that there is nothing to do. A uv-managed
        venv has no `pip` module, so fall back to `uv pip install` targeting
        this interpreter."""
        # Drop the pip subcommand verb and any flags, leaving package names:
        # "%pip install -q statsmodels" arrives here as ["install", "-q",
        # "statsmodels"].
        args = [a for a in args if not a.startswith("-") and a != "install"]
        missing = [a for a in args if not self._importable(a)]
        if not missing:
            return {"stdout": f"already installed: {', '.join(args)}",
                    "stderr": "", "result": None, "error": None}

        for command in ([sys.executable, "-m", "pip", "install", *missing],
                        ["uv", "pip", "install", "--python", sys.executable, *missing]):
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=900)
            except FileNotFoundError:
                continue
            if proc.returncode == 0:
                return {"stdout": proc.stdout[-2000:], "stderr": "", "result": None, "error": None}
            last = proc
        return {"stdout": "", "stderr": last.stderr[-2000:], "result": None,
                "error": f"could not install {missing}; add it to pyproject.toml and re-run `uv sync`"}

    @staticmethod
    def _importable(package: str) -> bool:
        """`pip install statsmodels` is a no-op if statsmodels imports. Only
        the plain package name is checked - a pinned spec falls through to a
        real install attempt, which is the safe direction to be wrong in."""
        import importlib.util
        if not package.isidentifier():
            return False
        return importlib.util.find_spec(package) is not None

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        shutil.rmtree(self.workdir, ignore_errors=True)
