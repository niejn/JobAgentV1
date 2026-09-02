from pathlib import Path

import pytest

from jobagent.skills import SkillManager
from jobagent.tools.skills import build_skill_tools

SKILL = """---
name: interview-helper
description: Prepare interview answers.
version: 1.0.0
---

# Interview helper

Do the thing.
"""


def test_manager_discovers_builtin_and_installed_skills(tmp_path: Path) -> None:
    builtins = tmp_path / "builtins"
    installed = tmp_path / "installed"
    (builtins / "builtin").mkdir(parents=True)
    (builtins / "builtin" / "SKILL.md").write_text(
        SKILL.replace("interview-helper", "builtin"), encoding="utf-8"
    )
    manager = SkillManager(installed, built_in_dir=builtins)

    assert [item.name for item in manager.list_skills()] == ["builtin"]
    assert manager.read_skill("builtin").startswith("---")


def test_install_validates_frontmatter_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source" / "SKILL.md"
    source.parent.mkdir()
    source.write_text(SKILL, encoding="utf-8")
    manager = SkillManager(tmp_path / "installed", built_in_dir=tmp_path / "none")

    first = manager.install(str(source))
    source.write_text(SKILL.replace("1.0.0", "2.0.0"), encoding="utf-8")
    second = manager.install(str(source))

    assert first.name == second.name == "interview-helper"
    assert second.installed is True
    assert manager.read_skill("interview-helper").count("2.0.0") == 1


def test_install_rejects_insecure_url_and_invalid_name(tmp_path: Path) -> None:
    manager = SkillManager(tmp_path / "installed", built_in_dir=tmp_path / "none")
    with pytest.raises(ValueError, match="HTTPS"):
        manager.install("http://example.com/SKILL.md")

    source = tmp_path / "SKILL.md"
    source.write_text(SKILL.replace("interview-helper", "../escape"), encoding="utf-8")
    with pytest.raises(ValueError, match="skill name"):
        manager.install(str(source))


@pytest.mark.asyncio
async def test_skill_tools_expose_discovery_read_and_install(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text(SKILL, encoding="utf-8")
    tools = {tool.name: tool for tool in build_skill_tools(SkillManager(tmp_path / "installed"))}

    result = await tools["install_skill"].ainvoke({"source": str(source)})
    assert result["status"] == "completed"
    listed = await tools["list_skills"].ainvoke({})
    assert "interview-helper" in {item["name"] for item in listed["skills"]}
    read = await tools["read_skill"].ainvoke({"name": "interview-helper"})
    assert read["status"] == "completed"
