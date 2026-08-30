"""Writes each run's outputs to disk (docs/requirements.md FR-8). Before
this, the final answer and trace only ever printed to the terminal and
were gone once the process exited - nothing was left to point someone at
afterward, and no individual number in the report could be traced back to
the real computation that produced it without re-reading scrollback."""

import json
from datetime import datetime
from pathlib import Path

from store import CLASSIFICATIONS, GROUPS, SCALES, json_safe

OUTPUTS_DIR = Path(__file__).parent / "outputs"


def write_outputs(
    handle_id: str,
    final_answer: str,
    trace_lines: list[str],
    token_usage: dict,
    step_count: int,
    tool_calls: list[dict],
    summarization_events: int = 0,
) -> Path:
    """Write one run's report, trace, and structured results to
    outputs/<handle_id>/<timestamp>/. Returns the directory written to.

    report.md is the plain-language answer (FR-8.1). trace.log is the
    exact tool-call/tool-result record already printed during the run,
    persisted instead of lost when the process exits (contributes to
    NFR-2's auditability, though the full audit-trail requirement is
    broader than this). results.json is the actual committed numbers
    behind the prose - classifications, scales, groups, scores, token
    usage, step count, per-tool-call latency (NFR-5), and how many times
    context summarization actually fired - so every claim
    in report.md can be checked against a real computation instead of
    just the prose text (FR-8.2/FR-8.3).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUTS_DIR / handle_id / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "report.md").write_text(final_answer)
    (run_dir / "trace.log").write_text("\n".join(trace_lines))

    results = {
        "handle_id": handle_id,
        "classifications": CLASSIFICATIONS.get(handle_id),
        "scales": SCALES.get(handle_id),
        "groups": GROUPS.get(handle_id),
        "token_usage": token_usage,
        "step_count": step_count,
        "tool_calls": tool_calls,
        "summarization_events": summarization_events,
    }
    (run_dir / "results.json").write_text(json.dumps(json_safe(results), indent=2))

    return run_dir
