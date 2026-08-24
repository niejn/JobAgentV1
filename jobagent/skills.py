"""Skill discovery for JobAgent (pure documentation pattern).

Skills are directories under ``skills/``, each containing only a ``SKILL.md``
with frontmatter metadata and workflow instructions.

Agent discovers skills via filesystem tools (``ls``, ``read_file``) and
executes steps described in the markdown using generic tools (``execute``,
``read_file``, etc.). No code loading is involved.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path(__file__).parent.parent.resolve() / "skills"


class SkillMetadata(BaseModel):
    """Frontmatter fields parsed from SKILL.md."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    author: str | None = None
    version: str | None = None


def _parse_skill_metadata(path: Path) -> SkillMetadata | None:
    """Parse SKILL.md frontmatter (YAML-style between --- delimiters)."""

    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return None
    frontmatter = "\n".join(lines[1:end_idx])
    try:
        data = yaml.safe_load(frontmatter)
    except Exception as exc:
        logger.warning("SKILL.md frontmatter parse error: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return SkillMetadata(
        name=str(data.get("name", path.parent.name)),
        description=str(data.get("description", "")),
        author=str(data["author"]) if data.get("author") else None,
        version=str(data["version"]) if data.get("version") else None,
    )


def discover_skills(skills_dir: Path | None = None) -> list[SkillMetadata]:
    """Scan the skills directory and return parsed metadata."""

    root = (skills_dir or DEFAULT_SKILLS_DIR).expanduser().resolve()
    if not root.is_dir():
        logger.info("Skills directory not found: %s", root)
        return []
    discovered: list[SkillMetadata] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        metadata = _parse_skill_metadata(skill_md)
        if metadata is None:
            continue
        discovered.append(metadata)
    return discovered