"""Agent tools for discovering, reading, and installing standard Skills."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.skills import SkillManager


class SkillName(BaseModel):
    name: str = Field(min_length=1, description="Skill directory name or frontmatter name")


class SkillSource(BaseModel):
    source: str = Field(
        min_length=1,
        description="Local path to SKILL.md/directory, or an HTTPS URL ending in SKILL.md",
    )


def build_skill_tools(manager: SkillManager) -> list[BaseTool]:
    """Build the minimal Skill lifecycle tools for the conversational Agent."""

    async def list_skills() -> dict[str, Any]:
        """List valid built-in and installed documentation Skills."""

        return {
            "status": "completed",
            "skills": [
                {
                    "name": item.name,
                    "skill_name": item.skill_name,
                    "description": item.description,
                    "version": item.version,
                    "installed": item.installed,
                }
                for item in manager.list_skills()
            ],
        }

    async def read_skill(name: str) -> dict[str, Any]:
        """Read the instructions of one discovered Skill."""

        record = manager.find_skill(name)
        if record is None:
            return {
                "status": "failed",
                "error_type": "skill_not_found",
                "message": f"skill not found: {name}",
            }
        try:
            content = record.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"status": "failed", "error_type": "read_failed", "message": str(exc)}
        return {
            "status": "completed",
            "name": name,
            "path": str(record.path.parent),
            "content": content,
        }

    async def install_skill(source: str) -> dict[str, Any]:
        """Install a standard Skill from a local source or HTTPS SKILL.md URL."""

        try:
            item = manager.install(source)
            return {
                "status": "completed",
                "name": item.name,
                "version": item.version,
                "installed_path": str(item.path.parent),
                "files": SkillManager.list_skill_files(item),
            }
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            return {"status": "failed", "error_type": "skill_install_failed", "message": str(exc)}


    return [
        StructuredTool.from_function(
            coroutine=list_skills,
            name="list_skills",
            description="Discover valid built-in and installed standard SKILL.md skills.",
        ),
        StructuredTool.from_function(
            coroutine=read_skill,
            name="read_skill",
            description="Read one standard SKILL.md by name before following its workflow.",
            args_schema=SkillName,
        ),
        StructuredTool.from_function(
            coroutine=install_skill,
            name="install_skill",
            description=(
                "Install a standard Skill from a local skill directory (SKILL.md plus "
                "bundled scripts/references), a local SKILL.md file, an HTTPS SKILL.md "
                "URL, or a GitHub repository URL (repo root or /tree/<branch>/<subpath> "
                "for multi-skill repos). Ask for user confirmation before installing "
                "and mention the file count when it is a directory."
            ),
            args_schema=SkillSource,
        ),
    ]
