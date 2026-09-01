"""
Long-term, cross-session memory (FR-4.3): durable facts that survive
between separate `uv run` invocations - unlike everything in store.py,
which only lives for one process. Backed by LangGraph's SqliteStore
(langgraph.store.sqlite), the framework's own answer to cross-thread
memory (same family as the checkpointer this project doesn't use, just
for facts instead of conversation state) - not a hand-rolled JSON file,
matching this project's existing call to use deepagents/LangGraph
machinery over hand-rolling wherever it already fits (see docs/progress.md,
"Architecture decisions made along the way").

Keyed by a schema signature, not handle_id: read_excel() in tools.py sets
handle_id to the filename stem, which breaks the moment a file is renamed
or re-exported - a schema signature (hash of sorted column:dtype pairs)
recognizes "the same file shape" across sessions regardless of filename.

Deliberately not exposed as a general "remember anything" tool. Schema-
keyed writes (classification/scale/grouping) happen automatically,
piggybacked on the same commit calls in agent_tools.py that already
decide something is a human-judgment override worth keeping, not a
mechanical suggestion re-derivable every time - see that file's
suggest-then-commit tools. set_preference_tool (agent_tools.py) is the
one explicit write, for user-stated preferences with no other natural
commit point to hang off of.
"""

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pandas as pd
from langgraph.store.sqlite import SqliteStore

DB_PATH = Path(__file__).parent / "memory" / "long_term.db"

# Conclusions accumulate one entry per run against the same schema -
# capped so recall() stays a small summary (FR-4.5), not a growing dump.
MAX_CONCLUSIONS = 5

# Guards every read-modify-write below (get existing dict, merge in a new
# key, put it back). LangGraph's tool node runs the several tool calls
# from one AI turn concurrently (confirmed live: a single turn issuing 10
# infer_scale_tool calls at once) - without this lock, two calls racing
# on the same store row (e.g. two columns' scales) can each read the
# same pre-update snapshot and the second put() clobbers the first's
# write, silently losing it. All calls happen within one process/run, so
# a plain in-process Lock is enough - this isn't cross-process contention.
_lock = threading.Lock()

_store: SqliteStore | None = None


def get_store() -> SqliteStore:
    """Open (or reuse) the on-disk long-term store. Lazy singleton, same
    pattern as agent_tools.py's _get_sandbox - created on first use. No
    matching close: a local sqlite connection costs nothing to leave open
    for the rest of the process, unlike the billable E2B sandbox that
    pattern also manages, so there's nothing worth an explicit
    close_store()."""
    global _store
    if _store is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        # isolation_level=None (autocommit): SqliteStore manages its own
        # BEGIN/COMMIT per batch - the default DBAPI behavior of
        # implicitly opening a transaction on the first statement
        # collides with that ("cannot start a transaction within a
        # transaction"), confirmed by hitting exactly that error without
        # this.
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        _store = SqliteStore(conn)
        _store.setup()
    return _store


def schema_signature(df: pd.DataFrame) -> str:
    """A key for 'this same file shape' that survives a rename, unlike
    handle_id. Columns are sorted so column order doesn't change the
    signature."""
    fingerprint = ",".join(sorted(f"{col}:{dtype}" for col, dtype in df.dtypes.items()))
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:12]


def recall(schema_sig: str) -> dict[str, Any]:
    """Everything remembered for this file schema: classification
    overrides, scale overrides (incl. reverse-coding), groupings, and up
    to the last MAX_CONCLUSIONS run summaries - or {"seen_before": False}
    if this exact schema has never been committed before."""
    items = get_store().search((schema_sig,))
    if not items:
        return {"seen_before": False}
    return {"seen_before": True, **{item.key: item.value for item in items}}


def remember_classification_overrides(schema_sig: str, overrides: dict[str, str]) -> None:
    store = get_store()
    with _lock:
        existing = store.get((schema_sig,), "classifications")
        merged = {**(existing.value if existing else {}), **overrides}
        store.put((schema_sig,), "classifications", merged)


def remember_scale(schema_sig: str, col: str, scale: dict) -> None:
    store = get_store()
    with _lock:
        existing = store.get((schema_sig,), "scales")
        merged = {**(existing.value if existing else {}), col: scale}
        store.put((schema_sig,), "scales", merged)


def remember_groups(schema_sig: str, groups: dict) -> None:
    with _lock:
        get_store().put((schema_sig,), "groups", groups)


def remember_conclusion(schema_sig: str, question: str, summary: str) -> None:
    store = get_store()
    with _lock:
        existing = store.get((schema_sig,), "conclusions")
        conclusions = existing.value.get("items", []) if existing else []
        conclusions = (conclusions + [{"question": question, "summary": summary}])[-MAX_CONCLUSIONS:]
        store.put((schema_sig,), "conclusions", {"items": conclusions})


def get_all_preferences() -> dict:
    item = get_store().get(("global",), "preferences")
    return item.value if item else {}


def set_preference(key: str, value: str) -> None:
    store = get_store()
    with _lock:
        existing = store.get(("global",), "preferences")
        merged = {**(existing.value if existing else {}), key: value}
        store.put(("global",), "preferences", merged)
