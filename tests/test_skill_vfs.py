"""Skill roots mounted into the agent VFS (native SkillsMiddleware hybrid)."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from jobagent.agent import (
    SKILL_BUILTIN_ROUTE,
    SKILL_INSTALLED_ROUTE,
    _mount_skill_roots,
    build_job_agent,
)
from jobagent.config import Settings
from jobagent.skills import SkillManager


def _make_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} demo description\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "run.py").write_text(
        "print('skill-script-ok')\n", encoding="utf-8"
    )


def _backend(tmp_path: Path):
    from deepagents.backends.local_shell import LocalShellBackend

    built_in = tmp_path / "builtin"
    installed = tmp_path / "installed"
    _make_skill(built_in, "demo-skill")
    _make_skill(installed, "extra-skill")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manager = SkillManager(installed, built_in_dir=built_in)
    shell = LocalShellBackend(root_dir=artifacts, virtual_mode=True)
    return _mount_skill_roots(shell, manager)


def test_skill_roots_visible_at_vfs_root(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    names = {entry["path"] for entry in backend.ls("/").entries}
    assert SKILL_BUILTIN_ROUTE in names
    assert SKILL_INSTALLED_ROUTE in names


def test_skill_files_readable_through_routes(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    listing = backend.ls(f"{SKILL_BUILTIN_ROUTE}demo-skill")
    assert any("SKILL.md" in entry["path"] for entry in listing.entries)
    scripts = backend.ls(f"{SKILL_BUILTIN_ROUTE}demo-skill/scripts")
    assert any("run.py" in entry["path"] for entry in scripts.entries)

    result = backend.read(f"{SKILL_BUILTIN_ROUTE}demo-skill/SKILL.md")
    assert "demo-skill demo description" in result.file_data["content"]


def test_execute_still_runs_on_the_shell_backend(tmp_path: Path) -> None:
    """Skill mounting must not break execute: it always routes to the default."""

    backend = _backend(tmp_path)
    response = backend.execute("echo skill-exec-ok")

    assert response.exit_code == 0
    assert "skill-exec-ok" in response.output


class _SystemCapturingModel(FakeMessagesListChatModel):
    """Records the final system message the middleware chain assembles."""

    captured_system: str = ""

    def bind_tools(self, tools: object, **kwargs: object) -> object:  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        for message in messages:
            if getattr(message, "type", "") == "system":
                self.captured_system = str(message.content)
                break
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _BindCapableModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> object:  # noqa: ARG002
        return self


def _vfs_settings(tmp_path: Path) -> Settings:
    installed = tmp_path / "installed"
    _make_skill(installed, "vfs-demo-skill")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
        jobagent_skills_dir=installed,
        jobagent_artifact_dir=artifacts,
    )


@pytest.mark.asyncio
async def test_skills_metadata_reaches_system_prompt(tmp_path: Path) -> None:
    """Native SkillsMiddleware exposes installed-skill metadata in the prompt."""

    settings = _vfs_settings(tmp_path)
    model = _SystemCapturingModel(responses=[AIMessage(content="ok")])
    agent = build_job_agent(settings, model=model, tools=[])
    try:
        graph = await agent._ensure_deep_agent()
        assert graph is not None
        await graph.ainvoke(
            {"messages": [("user", "hi")]},
            config={"configurable": {"thread_id": "skills-prompt"}},
        )
    finally:
        await agent.close()

    assert "## Skills System" in model.captured_system
    assert "vfs-demo-skill" in model.captured_system


@pytest.mark.asyncio
async def test_ls_tool_sees_mounted_skills(tmp_path: Path) -> None:
    """The filesystem tools browse skill dirs, including bundled scripts."""

    settings = _vfs_settings(tmp_path)
    model = _BindCapableModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ls",
                        "args": {"path": f"{SKILL_INSTALLED_ROUTE}vfs-demo-skill"},
                        "id": "call-ls-root",
                    },
                    {
                        "name": "ls",
                        "args": {
                            "path": f"{SKILL_INSTALLED_ROUTE}vfs-demo-skill/scripts"
                        },
                        "id": "call-ls-scripts",
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = build_job_agent(settings, model=model, tools=[])
    try:
        graph = await agent._ensure_deep_agent()
        assert graph is not None
        result = await graph.ainvoke(
            {"messages": [("user", "列出 skill 文件")]},
            config={"configurable": {"thread_id": "skills-ls"}},
        )
    finally:
        await agent.close()

    tool_outputs = [
        str(message.content) for message in result["messages"] if message.type == "tool"
    ]
    assert any("SKILL.md" in out for out in tool_outputs)
    assert any("run.py" in out for out in tool_outputs)


def test_memories_route_mounted_and_writable(tmp_path: Path) -> None:
    """Long-term memory (/memories/, Teacher_AICoding pattern): any session
    can read AND write the shared knowledge base through the VFS."""

    from deepagents.backends.local_shell import LocalShellBackend

    built_in = tmp_path / "builtin"
    installed = tmp_path / "installed"
    _make_skill(built_in, "demo-skill")
    _make_skill(installed, "extra-skill")
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    (memory_root / "channel-facts.md").write_text(
        "# facts\nWS acks are routinely lost.\n", encoding="utf-8"
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manager = SkillManager(installed, built_in_dir=built_in)
    shell = LocalShellBackend(root_dir=artifacts, virtual_mode=True)
    backend = _mount_skill_roots(shell, manager, memory_root)

    names = {entry["path"] for entry in backend.ls("/").entries}
    assert "/memories/" in names

    listing = backend.ls("/memories/")
    assert any("channel-facts.md" in entry["path"] for entry in listing.entries)
    read = backend.read("/memories/channel-facts.md")
    assert "routinely lost" in read.file_data["content"]

    backend.write("/memories/new-finding.md", "validated 2026-09-20")
    assert (memory_root / "new-finding.md").read_text(encoding="utf-8") == (
        "validated 2026-09-20"
    )


def test_memories_route_absent_without_root(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    names = {entry["path"] for entry in backend.ls("/").entries}
    assert "/memories/" not in names
