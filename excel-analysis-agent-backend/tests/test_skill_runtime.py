import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from prompts import build_gate_system_prompt, build_phase2_system_prompt
from skill_runtime import (
    SkillDisclosureSession,
    SkillRuntimeError,
    catalog_text,
    load_resource,
    load_skill,
    upload_scripts,
)


def test_level_1_catalog_contains_metadata_only():
    pack = load_skill()
    catalog = catalog_text(pack)

    assert pack.name in catalog
    assert pack.description in catalog
    assert "# Statistical Analysis" not in catalog
    assert "Required workflow" not in catalog
    assert "assumption_checks.py" not in catalog


def test_gate_prompt_has_level_1_but_not_level_2():
    pack = load_skill()
    prompt = build_gate_system_prompt(catalog_text(pack))

    assert pack.description in prompt
    assert pack.body not in prompt
    assert "Required workflow" not in prompt


def test_phase_2_contains_body_but_not_level_3_contents():
    pack = load_skill()
    reference = (pack.root / "references" / "test_selection_guide.md").read_text(encoding="utf-8")
    prompt = build_phase2_system_prompt(pack.name, pack.body, "handle_id: example")

    assert pack.body in prompt
    assert "ACTIVE STATISTICAL SKILL" in prompt
    assert "handle_id: example" in prompt
    assert reference not in prompt


def test_level_3_loads_one_allowlisted_resource():
    pack = load_skill()
    result = load_resource(pack, "references/test_selection_guide.md")

    assert result["path"] == "references/test_selection_guide.md"
    assert "Group comparisons" in result["content"]
    assert "Effect sizes and power" not in result["content"]


@pytest.mark.parametrize(
    "path",
    [
        "../SKILL.md",
        "scripts/assumption_checks.py",
        "/etc/passwd",
        "references/../../SKILL.md",
    ],
)
def test_level_3_rejects_traversal_and_script_source(path):
    with pytest.raises(SkillRuntimeError):
        load_resource(load_skill(), path)


def test_scripts_upload_without_entering_prompt_context(tmp_path):
    class FakeRuntime:
        def __init__(self):
            self.uploaded = []

        def sandbox_path_for(self, filename: str) -> str:
            return str(tmp_path / filename)

        def upload_file(self, local_path: str, remote_path: str) -> None:
            self.uploaded.append((Path(local_path), Path(remote_path)))

    runtime = FakeRuntime()
    uploaded = upload_scripts(load_skill(), runtime)

    assert any(Path(path).parts[-2:] == ("skill_scripts", "assumption_checks.py") for path in uploaded)
    assert runtime.uploaded[0][0].name == "assumption_checks.py"


def test_level_3_cannot_accumulate_the_full_pack():
    session = SkillDisclosureSession(load_skill(), max_distinct_resources=2)
    session.load("references/test_selection_guide.md")
    session.load("references/reporting_standards.md")

    with pytest.raises(SkillRuntimeError, match="context budget reached"):
        session.load("references/effect_sizes_and_power.md")

    # Re-reading an already disclosed resource is allowed and adds no context category.
    session.load("references/test_selection_guide.md")


def test_bundled_assumption_script_runs_on_grouped_data():
    script = load_skill().root / "scripts" / "assumption_checks.py"
    spec = importlib.util.spec_from_file_location("bundled_assumption_checks", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    data = pd.DataFrame(
        {
            "score": [1, 2, 3, 4, 5, 2, 3, 4, 5, 6],
            "group": ["A"] * 5 + ["B"] * 5,
        }
    )
    result = module.comprehensive_assumption_check(data, "score", "group")

    assert result["group_sizes"] == {"A": 5, "B": 5}
    assert result["homogeneity"]["test"] == "Levene"
