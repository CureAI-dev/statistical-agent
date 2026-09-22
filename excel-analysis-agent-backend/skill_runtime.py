"""Stats-only skill loading with enforced progressive disclosure.

Level 1 exposes frontmatter metadata, level 2 exposes one SKILL.md body,
and level 3 exposes one allowlisted reference/template/resource at a time.
Python scripts are copied into the existing analysis sandbox; their source
is not returned to the model as prompt context.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_SKILL_NAME = "statistical-analysis"
MAX_RESOURCE_CHARS = 12_000
MAX_DISTINCT_RESOURCES_PER_RUN = 2
RESOURCE_DIRS = frozenset({"references", "templates", "resources"})


class SkillRuntimeError(ValueError):
    """Raised when a skill pack is missing or violates its contract."""


@dataclass(frozen=True)
class SkillPack:
    name: str
    description: str
    body: str
    root: Path


class SkillDisclosureSession:
    """Bound level-3 growth so a run cannot accumulate the whole skill pack."""

    def __init__(
        self,
        pack: SkillPack,
        max_distinct_resources: int = MAX_DISTINCT_RESOURCES_PER_RUN,
    ) -> None:
        self.pack = pack
        self.max_distinct_resources = max_distinct_resources
        self._loaded: set[str] = set()

    @property
    def loaded_paths(self) -> frozenset[str]:
        return frozenset(self._loaded)

    def load(self, relative_path: str) -> dict[str, str | bool]:
        normalized = Path(relative_path).as_posix()
        if normalized not in self._loaded and len(self._loaded) >= self.max_distinct_resources:
            raise SkillRuntimeError(
                "Level-3 context budget reached. Use the resources already loaded; "
                "the runtime will not accumulate the full skill pack."
            )
        result = load_resource(self.pack, normalized)
        self._loaded.add(normalized)
        return result

    def reset(self) -> None:
        self._loaded.clear()


class SandboxRuntime(Protocol):
    def sandbox_path_for(self, filename: str) -> str: ...

    def upload_file(self, local_path: str, remote_path: str) -> None: ...


def skill_root() -> Path:
    configured = (os.getenv("STAT_SKILL_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "skill"


def _parse_frontmatter(text: str, source: Path) -> tuple[dict[str, str], str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, flags=re.DOTALL)
    if not match:
        raise SkillRuntimeError(f"{source}: SKILL.md must start with YAML frontmatter")

    raw, body = match.groups()
    metadata: dict[str, str] = {}
    current_key: str | None = None
    folded: list[str] = []

    def flush() -> None:
        nonlocal current_key, folded
        if current_key is not None:
            metadata[current_key] = " ".join(part.strip() for part in folded if part.strip())
        current_key = None
        folded = []

    for line in raw.splitlines():
        if line.startswith((" ", "\t")) and current_key:
            folded.append(line)
            continue
        flush()
        if ":" not in line:
            raise SkillRuntimeError(f"{source}: invalid frontmatter line {line!r}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value in {">", ">-", "|", "|-"}:
            current_key = key
        else:
            metadata[key] = value.strip("\"'")
    flush()
    return metadata, body.strip()


def load_skill(name: str = DEFAULT_SKILL_NAME) -> SkillPack:
    path = skill_root() / name / "SKILL.md"
    if not path.is_file():
        raise SkillRuntimeError(f"Statistical skill not found: {path}")
    metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8"), path)
    parsed_name = metadata.get("name", "")
    description = metadata.get("description", "")
    if parsed_name != name:
        raise SkillRuntimeError(f"{path}: frontmatter name must be {name!r}, got {parsed_name!r}")
    if not description:
        raise SkillRuntimeError(f"{path}: description is required")
    if len(description) > 1024:
        raise SkillRuntimeError(f"{path}: description exceeds 1024 characters")
    if not body:
        raise SkillRuntimeError(f"{path}: markdown body is required")
    return SkillPack(name=parsed_name, description=description, body=body, root=path.parent)


def catalog_text(pack: SkillPack) -> str:
    """Level 1: metadata only. Never includes the markdown body."""
    return f"- {pack.name}: {pack.description}"


def load_resource(pack: SkillPack, relative_path: str) -> dict[str, str | bool]:
    """Level 3: return exactly one allowlisted text resource."""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SkillRuntimeError("Skill resource path must stay inside the active skill")
    if not relative.parts or relative.parts[0] not in RESOURCE_DIRS:
        allowed = ", ".join(sorted(RESOURCE_DIRS))
        raise SkillRuntimeError(f"Skill resources must be under one of: {allowed}")
    path = (pack.root / relative).resolve()
    if pack.root.resolve() not in path.parents or not path.is_file():
        raise SkillRuntimeError(f"Skill resource not found: {relative_path}")
    if path.suffix.lower() not in {".md", ".txt"}:
        raise SkillRuntimeError("Only markdown and text skill resources may enter model context")
    content = path.read_text(encoding="utf-8")
    truncated = len(content) > MAX_RESOURCE_CHARS
    if truncated:
        content = content[:MAX_RESOURCE_CHARS] + "\n\n[resource truncated by runtime]"
    return {"path": relative.as_posix(), "content": content, "truncated": truncated}


def upload_scripts(pack: SkillPack, runtime: SandboxRuntime) -> list[str]:
    """Copy scripts to sandbox without placing their source in model context."""
    scripts_dir = pack.root / "scripts"
    if not scripts_dir.is_dir():
        return []
    uploaded: list[str] = []
    for script in sorted(scripts_dir.glob("*.py")):
        remote = runtime.sandbox_path_for(f"skill_scripts/{script.name}")
        runtime.upload_file(str(script), remote)
        uploaded.append(remote)
    return uploaded
