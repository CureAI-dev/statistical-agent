# Plan: Statistical skill pack for the analysis agent

**Status:** Implemented 2026-09-22 (slim runtime described in §12).  
**Scope:** One kind of skill — **statistical analysis** (test choice, assumptions, effect sizes, reporting). Not a general plugin marketplace. Not Cursor-only `SKILL.md` loading.  
**Target runtime:** `excel-analysis-agent-backend` (job = dataset + analysis request → tools + sandbox → report).

This document describes folder layout, what lives in `SKILL.md`, and **progressive disclosure** so the model **never** receives the full skill pack in one shot.

---

## 1. Goal

When a job starts, the statistical agent should be able to **use** a vendored statistical skill:

- **Prompts / workflow** from `SKILL.md` (only after it decides the skill applies)
- **Scripts** (e.g. assumption checks) in the **sandbox**, on demand
- **Templates** (APA report snippets) when writing the report
- **Resources** (test-selection tables, power notes, Bayesian notes) **one file at a time**

The agent already has tools (`read_excel`, `profile`, `recommend_test`, `run_code`, Likert scoring). The skill **does not replace** those tools. It **teaches** when to call them, which test to prefer when the table is too coarse, how to check assumptions, and how to write results.

---

## 2. Non-goals (explicit)

- Loading arbitrary skills from the internet (`skills.sh`, React, Git, Figma).
- Dumping `SKILL.md` + all `references/` + all scripts into the system prompt at boot.
- Running skill Python on the host process (`exec` / local pandas). Scripts run in the **same sandbox** as `run_code_tool`.
- Replacing `recommend_test()` in v1. The table stays; the skill **guides and extends** it.
- Changing the Cognitive Pal Stat FastAPI template engine in this milestone (later: same pack can inform `dynamic_agent` / planner).

---

## 3. Progressive disclosure (hard rule)

**All skill context must not be in the model context at once.** Three levels, always in order. The runtime must enforce this (not “the model is asked to be brief”).

```
Level 1  catalog          →  name + description only
Level 2  SKILL.md body    →  workflow, quick tables, pointers to L3
Level 3  one resource     →  one references/*.md OR one template OR one script
```

| Level | When it is in context | What is included | What is forbidden |
|-------|------------------------|------------------|-------------------|
| **1 — Discovery** | Every job, from the first LLM turn | YAML `name` + `description` (and maybe `enabled`) | `SKILL.md` body, any `references/`, any script source, any template body |
| **2 — Activate** | Only after the model (or a deterministic gate) **chooses** this skill for this job | `SKILL.md` **markdown body** (frontmatter already used at L1) | Full `references/` tree, concatenated scripts, extra skills’ bodies |
| **3 — Deepen** | Only when L2 says “see file X” **and** the current step needs it | **Exactly one** extra file per tool call (or a short named section) | “Load entire `references/`”, “paste assumption_checks.py + Bayesian guide + APA templates together” |

### Who decides to go to the next level

1. **Level 1 → 2:** The gate / phase-2 model sees the catalog. It requests activation when the job is statistical analysis (compare groups, association, regression, reliability, power, APA write-up). A **safe default**: if the job is already in this agent, activate `statistical-analysis` **once** after `submit_plan` is `ready` — still **do not** load L3 yet.
2. **Level 2 → 3:** The activated `SKILL.md` body lists **paths**, not file contents. The model calls a dedicated tool, e.g. `load_skill_resource(skill_id, path)`, with an allowlist. The tool returns **that file only**. Scripts are **not** pasted into the prompt; they are **uploaded to the sandbox** and invoked via `run_code_tool`.

### Caps (to keep this 100% true)

- Max **one** skill body (L2) in context per run unless we later add a second stats pack (then still one body at a time, or a tiny catalog of names).
- Max **one** L3 markdown/template payload in the **latest** tool result; older L3 payloads may be dropped or summarized by existing compression middleware — do not re-inject them on every turn.
- Skill script **source** is not in the LLM context unless the model explicitly loads a **short** excerpt; default is “file exists at `/sandbox/skill/assumption_checks.py`, import it.”
- Token budget: L1 descriptions ≤ 1024 chars each (skill spec). L2 `SKILL.md` body target **&lt; 500 lines**. L3 files stay in `references/` so they are never concatenated.

If a future change would concatenate L2 + all L3 files into `SYSTEM_PROMPT`, it **violates this plan**.

---

## 4. Folder organization

Proposed root (inside the statistical-agent repo):

```
statistical-agent/
  skill/                                    # configurable pack root (name is literal: skill/)
    statistical-analysis/                   # first (and initially only) pack
      SKILL.md                              # frontmatter + L2 body
      skill.yaml                            # optional runtime flags (enabled, sandbox extras)
      references/                           # L3 markdown — load one at a time
        test_selection_guide.md
        assumptions_and_diagnostics.md
        effect_sizes_and_power.md
        bayesian_statistics.md
        reporting_standards.md
      scripts/                              # L3 code — copy into sandbox, do not dump into prompt
        assumption_checks.py
      templates/                            # L3 report snippets — load when writing the report
        independent_t_test.md
        oneway_anova.md
        multiple_regression.md
        bayesian_comparison.md
      resources/                            # optional: data dictionaries, citation snippets, license
        CITATION.md                         # K-Dense / skill paper cite if the run used the pack
```

**What each directory is for**

| Path | Role | Disclosure level |
|------|------|------------------|
| `skill/` | Only place the agent scans for stats packs | — |
| `SKILL.md` | Identity + when-to-use (YAML) + **instructions and patterns** (body) | L1 = YAML; L2 = body |
| `skill.yaml` | `enabled`, sandbox pip extras (`pingouin`, …), resource allowlist | Host config, not model context (except `enabled` via catalog) |
| `references/` | Long guides. Linked from SKILL.md by **filename** | L3 |
| `scripts/` | Executable helpers. Host copies into sandbox | L3 (path + how to call, not full source) |
| `templates/` | APA / report skeletons. One template per test family | L3 |
| `resources/` | License, citation, small static assets | L3 if needed |

**Starting content:** copy from workspace skill  
`cognitive-pal/.agents/skills/statistical-analysis/`  
then **split** the current fat `SKILL.md` (code samples, APA examples, install notes) into `references/` and `templates/` so L2 stays a **workflow + pointers**.

---

## 5. What goes in `SKILL.md`

Two parts. They are **loaded at different levels**.

### 5.1 YAML frontmatter (Level 1 only)

Required for discovery. This is the **only** skill text in the initial system / catalog message.

```yaml
---
name: statistical-analysis
description: >-
  Guided statistical analysis for research data: test selection, assumption
  checking, effect sizes, power, Bayesian alternatives, and APA-formatted
  reporting. Use when the job compares groups, tests a hypothesis, analyzes
  survey or experimental tables, checks assumptions, computes sample size,
  or writes results — even if the request does not name a test. Covers
  t-tests, ANOVA, chi-square, correlation, regression, non-parametric and
  Bayesian methods.
---
```

| Field | Rules | Used for |
|-------|--------|----------|
| `name` | lowercase, hyphens, ≤ 64 chars, unique under `skill/` | Catalog id, sandbox path prefix |
| `description` | ≤ 1024 chars, third person, **what + when** | Level 1 context **only** |

Optional later (still L1 or host-only, not L2 body): `version`, `license`.

**Do not** put workflow, code, or tables in the description.

### 5.2 Markdown body (Level 2 only)

Loaded **after** activation. Contains **instructions and code patterns**, but **not** the full library of references.

The body should include:

1. **Overview** — one short paragraph: defensible analysis (right test, assumptions, effect size, write-up).
2. **When this pack applies** — bullets matching this agent’s jobs (group comparison, association, Likert scoring then test, power, report).
3. **Workflow (ordered)** — the six steps already in the upstream skill: frame → inspect → select test → check assumptions → run test + effect size → report. Map each step to **existing tools** (`profile_tool`, `recommend_test_tool`, `run_code_tool`, `score_items_tool`).
4. **Quick test map** — a **short** table (two groups / 3+ / association / regression). Details: “for factorial, survival, reliability, counts → load `references/test_selection_guide.md`”.
5. **Assumption rule** — “before interpreting a parametric test, run `scripts/assumption_checks.py` in the sandbox (see how-to below). If violated, switch test and **say you switched**.”
6. **Code patterns (compact)** — **snippets**, not full tutorials: e.g. `pg.ttest(..., correction='auto')`, `pg.anova` + Tukey, `statsmodels` OLS + diagnostics. Pingouin ≥0.6 column names (`p_val`, `cohen_d`). Full install/compat notes live in L3 (`references/assumptions_and_diagnostics.md` or a short `references/libraries.md`).
7. **Integrity rules** — planned vs exploratory, no p-hacking, report non-significant results, effect size over “p &lt; .05” theater.
8. **Level-3 index (paths only)** — mandatory. Example:

```markdown
## Load only what the current step needs

- Test choice beyond the quick map → `references/test_selection_guide.md`
- Failed Shapiro/Levene / what to do → `references/assumptions_and_diagnostics.md`
- Cohen's d / η² / power → `references/effect_sizes_and_power.md`
- Bayesian t / ANOVA → `references/bayesian_statistics.md`
- APA wording → `references/reporting_standards.md` **or** one file in `templates/`
- Assumption code → sandbox import `assumption_checks` (do not paste the module)
```

9. **How to call scripts** — 10–20 lines: copy is already at `skill_scripts/assumption_checks.py`; example:

```python
from assumption_checks import comprehensive_assumption_check
comprehensive_assumption_check(data=df, value_col="...", group_col="...", plot=False)
```

**Keep the body under ~500 lines.** Move APA full examples into `templates/`. Move Bayesian priors and power formulas into `references/`.

---

## 6. Scripts, templates, resources — contents

### `scripts/`

| File | Purpose | How the agent uses it |
|------|---------|------------------------|
| `assumption_checks.py` | Shapiro–Wilk, Levene, outliers, OLS diagnostics | Uploaded into sandbox at job start **or** on first need; invoked via `run_code_tool`. Source **not** in the prompt. |

Later (optional): thin wrappers that call pingouin/statsmodels with the skill’s preferred options — still L3, still sandbox-only.

Sandbox extras (from `skill.yaml`): e.g. `pingouin`, matching the skill’s install notes. Host does not run these imports.

### `templates/`

Short markdown skeletons with placeholders (`n`, `M`, `SD`, `t`, `df`, `p`, `d`, CI). One file per family so L3 is small:

- Independent t-test  
- One-way ANOVA + Tukey  
- Multiple regression  
- Bayesian group comparison  

The body of `SKILL.md` points to these; it does **not** include all four full APA examples.

### `resources/`

- Citation for the scientific-agent-skills paper **if** the pack materially contributed (as the upstream skill requires).  
- License text.  
Not injected unless the report stage asks for citations.

### `references/`

Existing five guides from the upstream skill, unchanged in role: **L3 only**, one file per `load_skill_resource` call.

---

## 7. Runtime behavior (implementation later)

Conceptual flow — **not** to be coded until this plan is signed off:

1. **Boot / job start:** Scan `skill/*/SKILL.md`. Parse YAML. Build catalog: `[{name, description}]`. Inject catalog into the model as **Level 1** (a short block, not the files).
2. **Gate phase:** Unchanged tools (`read_excel`, `profile`, `submit_plan`). Catalog may be visible so the model knows a stats pack exists. **Do not** attach L2 yet (gate is “can we analyze?”, not “run Shapiro”).
3. **Phase 2 start (plan ready):** Activate `statistical-analysis` (default for this product). Tool or host appends **L2 body once**. Register `load_skill_resource` + ensure sandbox has `scripts/` on `PYTHONPATH`.
4. **During analysis:** Model follows L2 workflow. Before a t-test/ANOVA, it loads **at most** `references/assumptions_and_diagnostics.md` **or** runs the assumption script. Before an unusual design, it loads `test_selection_guide.md` only. Before the final answer, it loads **one** template.
5. **Report:** Numbers still **only** from tool/sandbox results (existing hard rule in `SYSTEM_PROMPT`). Skill templates constrain **wording**, not invented statistics.

### Suggested tools (later)

| Tool | Allowed arguments | Returns |
|------|-------------------|---------|
| `list_skills` | — | L1 catalog |
| `activate_skill` | `name` | L2 body of that skill (once) |
| `load_skill_resource` | `name`, `relative_path` | File text if path is under that skill’s `references/` or `templates/` or `resources/` |

Path traversal outside the skill directory is rejected. `scripts/*.py` is **not** returned as a giant string by default; `activate_skill` or sandbox setup copies files.

---

## 8. Configurability

- **Add another statistical pack:** new folder `skill/survey-reliability/` with the same shape (`SKILL.md` + optional refs/scripts). Catalog grows by **one description**. Still no generic plugins.
- **Disable:** `skill.yaml` `enabled: false` or omit the folder.
- **Root path:** default `statistical-agent/skill/`; override with env e.g. `STAT_SKILL_DIR` so deployments can point at a mounted pack without code changes.

---

## 9. Mapping from today’s agent

| Existing piece | After this plan |
|----------------|-----------------|
| `prompts.py` `SYSTEM_PROMPT` | Stays; plus L1 catalog; L2 appended only on activate |
| `recommend_test()` | Stays as default lookup; L2/L3 may justify a different test and `run_code` |
| `run_code_tool` / E2B | Unchanged; skill scripts execute here |
| Likert classify / score | Unchanged; skill workflow **after** scores exist |
| Context compression | Still applies; L3 must not fight it by re-pasting all refs |

---

## 10. Implementation order (when approved)

1. Vendor/copy pack into `skill/statistical-analysis/` and **split** fat SKILL.md → L2 body + L3 files.  
2. Parser: frontmatter vs body; catalog builder (L1).  
3. `activate_skill` + inject L2 once per run.  
4. `load_skill_resource` allowlist (L3).  
5. Sandbox copy of `scripts/`.  
6. Prompt one-liners: “use catalog; activate then load one resource; never request all files.”  
7. Tests: L1 payload has no body; activate does not include `references/`; resource tool cannot read `../`.  

This document is **step 0**. No loader code until the layout and disclosure rules above are agreed.

---

## 11. Prompt restructure (skill has method weight)

**Problem today.** `SYSTEM_PROMPT` is one ~200-line blob. Likert tooling, sandbox hygiene, memory, and **method** (Shapiro → `recommend_test_tool` → scipy) are mixed. If you paste the skill at the end, the model still obeys the **earlier, repeated** host rule (“never pick a test from memory; always `recommend_test_tool`”). The skill cannot win.

**Fix:** stop using one prompt. **Assemble** phase-2 context in layers, with an explicit conflict rule. Do **not** dump L3 into this assembly.

### Precedence (write this in the prompt, do not imply it)

| Rank | Layer | Who authors it | Wins when |
|------|--------|----------------|-----------|
| 1 (never overridden) | **Host invariants** | `prompts.py` | Numbers, sandbox, retries, task-list honesty |
| 2 | **Active skill (L2 body)** | `skill/*/SKILL.md` markdown | Which test, assumptions, effect sizes, APA wording, when to load L3 |
| 3 | **Host platform** | `prompts.py` | How to call *this* repo’s tools (classify, score, formulas, memory) |

One sentence the model must see **immediately before** the skill body:

> On statistical method (test choice, assumptions, effect sizes, reporting), the ACTIVE SKILL section below overrides any other method advice in this prompt. On numeric honesty, sandbox paths, and tool retry behavior, the HOST INVARIANTS section above overrides the skill.

### How to assemble `phase2_system_prompt` (v1)

```
[1] HOST INVARIANTS     ~40–60 lines, always
[2] SKILL PRECEDENCE    2–3 sentences, always
[3] ACTIVE SKILL (L2)   SKILL.md body, host-injected after gate ready
[4] HOST PLATFORM       tool mechanics, Likert, memory, formulas
[5] PINNED RUN STATE    task list, handle_id, sandbox_path, data dictionary
```

Gate phase **does not** get L2. `GATE_SYSTEM_PROMPT` stays “can we analyze / task list only.”

### What leaves the current `SYSTEM_PROMPT`

**Keep as Host invariants (short, first):**

- Never invent a number; only tool/sandbox results.
- Do not re-call a tool that already said non-transient failure.
- Sandbox path vs original path; `df` loaded once; seed randomness.
- Do not silently continue a task list the data contradicted.
- `read_file` vs `read_excel_tool` after summarization.

**Move into the skill L2 (method — this is the weight shift):**

- Whether and how to check normality / variance.
- When parametric vs non-parametric / Welch / Fisher.
- Effect sizes and CIs.
- What the final statistical write-up must contain (test, assumptions, *d*/η², plain meaning).
- Pointers to L3 (`load_skill_resource`, assumption script).

**Delete from the host prompt** (otherwise they **beat** the skill):

- “Always Shapiro, then **always** `recommend_test_tool`, then **exactly** that scipy test.”
- “Final answer = test + assumptions + p-value” as the **only** report shape (skill templates are richer).

Replace those with one host line under platform:

> For a named lookup that matches the skill’s quick map, you may call `recommend_test_tool`. If the skill’s workflow names a different test (paired, ordinal, reliability, Welch, etc.), **follow the skill** and run it with `run_code_tool`. Do not run a table test that the skill just rejected.

**Keep as Host platform (after the skill, still necessary):**

- Structural issues on read, column rename for formulas, Likert classify / bulk `infer_scales_tool` / group / `score_items_tool` (token-budget lessons).
- `recall_memory_tool` / preferences.
- Low-variance columns.
- “File already loaded; do not `read_excel_tool` again.”

Those are **this agent’s** scars. The skill does not know them. They must not be **deleted**; they must not sit **on top of** method.

### Why this gives the skill more weight (without dumping L3)

1. **Position:** method block is early, not after 150 lines of Likert.
2. **Conflict rule:** written twice — once in invariants, once as the header of layer 3.
3. **No competing method paragraph** in the host prompt.
4. **Volume:** L2 is the longest *method* text; host invariants stay short. L3 still loads one file at a time.

### What not to do

- Do not prepend the skill and leave the old Shapiro/`recommend_test` paragraph in place.
- Do not put Bayesian/PyMC in L2 unless the sandbox has those packages.
- Do not inject all `references/` to “give the skill more weight” — that is more tokens, not more authority, and it fights compression.

### Implementation note (when coding)

`prompts.py` should export **fragments** (`HOST_INVARIANTS`, `HOST_PLATFORM`, `skill_precedence(name)`) plus `build_phase2_system_prompt(skill_body: str, pinned_state: str) -> str`. Do not keep a single `SYSTEM_PROMPT` string that already contains method advice.

---

## 12. Implemented v1

- `skill_runtime.py` parses frontmatter/body, builds L1 metadata, exposes one
  allowlisted L3 file per call, rejects traversal/script-source reads, and copies
  scripts into the sandbox.
- Gate receives only `name + description`.
- Phase 2 deterministically activates the one bundled statistics skill and
  assembles: host invariants → precedence → skill body → host platform → pinned
  run state.
- `load_skill_resource_tool` is the sole L3 tool. A run may accumulate at most
  **two distinct** L3 files; this prevents the complete pack from entering
  context while allowing one method/assumption guide and one reporting template.
- `assumption_checks.py` is uploaded to `skill_scripts/` and its directory is
  added to sandbox `sys.path`; script source is not returned to the model.
- Bayesian guidance is reference-only and requires confirmed dependencies.
- `STAT_SKILL_DIR` may override the default `statistical-agent/skill/` root.
