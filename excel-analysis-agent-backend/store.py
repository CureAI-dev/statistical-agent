"""
Committed state for one agent run: the real dataframes (never sent to the
model - see agent_tools.py) plus whatever each pipeline step has decided,
keyed by handle_id (and handle_id -> column for per-item results). Later
steps read from here instead of re-deriving something, or re-asking the
model to remember its own past decisions.
"""

import math
from typing import Any

import numpy as np

# The real, possibly-huge DataFrames, keyed by a short id string. The model
# only ever sees the id and a summary - never the actual rows - which is
# what "handle" means in the requirements doc.
HANDLES: dict = {}

# The sandbox path each handle_id's working copy lives at (set by
# read_excel_tool). score_items_tool re-uploads the dataframe here after
# adding score columns, so run_code_tool sees them without the model having
# to recreate them by hand.
SANDBOX_PATHS: dict = {}

# Committed column classification per handle_id (see classify_columns_tool
# in agent_tools.py) - the place later steps (grouping, scoring) read
# "which columns are Likert items" from.
CLASSIFICATIONS: dict = {}

# Committed Likert scale per handle_id -> column (see infer_scale_tool) -
# the place later steps (grouping, scoring) read each item's point count,
# label->score map, and reverse-coding from.
SCALES: dict = {}

# Committed subscale grouping per handle_id (see group_items_tool) - the
# place score_items_tool reads "which items make up which subscale" from.
GROUPS: dict = {}


def json_safe(value: Any) -> Any:
    """
    pandas/numpy stats (df.describe(), null counts, etc.) come back as
    numpy int64/float64, and sometimes NaN - none of which the model's
    tool-result JSON can carry as-is. Recursively convert everything to
    plain Python types so the result can be sent back to the model.

    Returns `Any` on purpose: this walks an arbitrary nested structure and
    the shape of what comes out mirrors whatever went in.
    """
    if isinstance(value, dict):
        return {key: json_safe(v) for key, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value
