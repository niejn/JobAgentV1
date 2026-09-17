import io
import zipfile
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


def test_load_subagent_skill_returns_content_or_degrades(tmp_path: Path) -> None:
    from jobagent.agent import _load_subagent_skill

    builtins = tmp_path / "builtins"
    (builtins / "xhs-recruitment-email").mkdir(parents=True)
    (builtins / "xhs-recruitment-email" / "SKILL.md").write_text(
        SKILL.replace("interview-helper", "xhs-recruitment-email"), encoding="utf-8"
    )
    manager = SkillManager(tmp_path / "installed", built_in_dir=builtins)

    content = _load_subagent_skill(manager, "xhs-recruitment-email")
    assert content is not None
    assert "# Interview helper" in content
    assert "---" not in content.splitlines()[0]
    assert _load_subagent_skill(manager, "missing-skill") is None


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


def _make_directory_skill(root: Path, name: str = "doc-maker") -> Path:
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "SKILL.md").write_text(
        SKILL.replace("interview-helper", name), encoding="utf-8"
    )
    (skill_dir / "scripts" / "build.py").write_text("print('building')\n", encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (skill_dir / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return skill_dir


def test_install_directory_copies_bundled_files(tmp_path: Path) -> None:
    source = _make_directory_skill(tmp_path)
    installed = tmp_path / "installed"
    manager = SkillManager(installed, built_in_dir=tmp_path / "none")

    record = manager.install(str(source))

    assert record.installed is True
    skill_root = installed / "doc-maker"
    assert (skill_root / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (skill_root / "scripts" / "build.py").exists()
    assert (skill_root / "references" / "guide.md").exists()
    assert (skill_root / "icon.png").read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert manager.find_skill("doc-maker").path == skill_root / "SKILL.md"
    assert SkillManager.list_skill_files(record) == [
        "SKILL.md",
        "icon.png",
        "references/guide.md",
        "scripts/build.py",
    ]


def test_install_directory_overwrites_existing_skill(tmp_path: Path) -> None:
    source = _make_directory_skill(tmp_path)
    installed = tmp_path / "installed"
    manager = SkillManager(installed, built_in_dir=tmp_path / "none")
    manager.install(str(source))

    (source / "scripts" / "build.py").write_text("print('v2')\n", encoding="utf-8")
    (source / "scripts" / "extra.py").write_text("pass\n", encoding="utf-8")
    manager.install(str(source))

    skill_root = installed / "doc-maker"
    assert (skill_root / "scripts" / "build.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert (skill_root / "scripts" / "extra.py").exists()
    assert not list(installed.glob(".doc-maker-*")), "staging dirs must be cleaned up"


def test_install_directory_rejects_invalid_trees(tmp_path: Path) -> None:
    manager = SkillManager(tmp_path / "installed", built_in_dir=tmp_path / "none")

    no_skill_md = tmp_path / "no-skill-md"
    no_skill_md.mkdir()
    (no_skill_md / "README.md").write_text("no skill here", encoding="utf-8")
    with pytest.raises(ValueError, match="SKILL.md"):
        manager.install(str(no_skill_md))

    hidden = _make_directory_skill(tmp_path, "hidden-payload")
    (hidden / ".DS_Store").write_bytes(b"junk")
    with pytest.raises(ValueError, match="hidden"):
        manager.install(str(hidden))

    executable = _make_directory_skill(tmp_path, "with-exe")
    (executable / "scripts" / "helper.exe").write_bytes(b"MZ")
    with pytest.raises(ValueError, match="forbidden file type"):
        manager.install(str(executable))

    oversize = _make_directory_skill(tmp_path, "oversize")
    (oversize / "SKILL.md").write_text(
        SKILL.replace("interview-helper", "oversize") + "x" * 600_000, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="size limit"):
        manager.install(str(oversize))


def test_install_directory_rejects_symlinks(tmp_path: Path) -> None:
    link_target = tmp_path / "outside.txt"
    link_target.write_text("outside", encoding="utf-8")
    try:
        source = _make_directory_skill(tmp_path, "with-link")
        (source / "scripts" / "leak.txt").symlink_to(link_target)
    except OSError:
        pytest.skip("creating symlinks requires elevated privileges on this platform")

    manager = SkillManager(tmp_path / "installed", built_in_dir=tmp_path / "none")
    with pytest.raises(ValueError, match="symlinks are not allowed"):
        manager.install(str(source))


@pytest.mark.asyncio
async def test_skill_tools_return_path_and_files(tmp_path: Path) -> None:
    source = _make_directory_skill(tmp_path)
    tools = {
        tool.name: tool
        for tool in build_skill_tools(SkillManager(tmp_path / "installed"))
    }

    installed = await tools["install_skill"].ainvoke({"source": str(source)})
    assert installed["status"] == "completed"
    assert installed["files"] == [
        "SKILL.md",
        "icon.png",
        "references/guide.md",
        "scripts/build.py",
    ]
    assert installed["installed_path"].endswith("doc-maker")

    read = await tools["read_skill"].ainvoke({"name": "doc-maker"})
    assert read["status"] == "completed"
    assert read["path"].endswith("doc-maker")
    assert read["content"].startswith("---")


def _make_github_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_parse_github_url_variants() -> None:
    from jobagent.skills import _parse_github_url

    assert _parse_github_url("https://github.com/owner/repo") == (
        "owner", "repo", "main", "",
    )
    assert _parse_github_url("https://github.com/owner/repo.git") == (
        "owner", "repo", "main", "",
    )
    assert _parse_github_url(
        "https://github.com/owner/repo/tree/main/skills/foo"
    ) == ("owner", "repo", "main", "skills/foo")
    assert _parse_github_url("https://example.com/owner/repo") is None
    assert _parse_github_url("https://github.com/owner") is None


def test_extract_skill_from_archive_locates_root_and_subpath(tmp_path: Path) -> None:
    from jobagent.skills import SkillManager

    archive = _make_github_zip(
        {
            "repo-main/.gitignore": b"*.pyc\n",
            "repo-main/SKILL.md": SKILL.encode(),
            "repo-main/scripts/build.py": b"print('ok')\n",
            "repo-main/skills/foo/SKILL.md": SKILL.replace(
                "interview-helper", "foo-skill"
            ).encode(),
        }
    )
    extract_root = tmp_path / "extract"
    extract_root.mkdir()

    root_dir = SkillManager._extract_skill_from_archive(archive, "", extract_root)
    assert (root_dir / "SKILL.md").exists()
    assert not (extract_root / ".gitignore").exists(), "hidden entries must be dropped"

    extract_root2 = tmp_path / "extract2"
    extract_root2.mkdir()
    sub_dir = SkillManager._extract_skill_from_archive(archive, "skills/foo", extract_root2)
    assert (sub_dir / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_extract_skill_from_archive_rejects_traversal_and_missing_skill(
    tmp_path: Path,
) -> None:
    from jobagent.skills import SkillManager

    extract_root = tmp_path / "extract"
    extract_root.mkdir()
    traversal = _make_github_zip({"repo-main/../../evil.txt": b"boom"})
    with pytest.raises(ValueError, match="unsafe archive entry"):
        SkillManager._extract_skill_from_archive(traversal, "", extract_root)

    no_skill = _make_github_zip({"repo-main/skills/one/SKILL.md": SKILL.encode()})
    with pytest.raises(ValueError, match="candidates: skills/one/SKILL.md"):
        SkillManager._extract_skill_from_archive(no_skill, "", extract_root)


def test_install_github_downloads_and_installs_tree(tmp_path: Path, monkeypatch) -> None:
    manager = SkillManager(tmp_path / "installed", built_in_dir=tmp_path / "none")
    archive = _make_github_zip(
        {
            "repo-main/.gitignore": b"*.pyc\n",
            "repo-main/frida-workflow/SKILL.md": SKILL.replace(
                "interview-helper", "frida-workflow"
            ).encode(),
            "repo-main/frida-workflow/scripts/check.py": b"print('ok')\n",
            "repo-main/frida-workflow/references/refs.md": b"# refs\n",
        }
    )
    monkeypatch.setattr(
        SkillManager, "_download_github_archive", lambda self, o, r, b: archive
    )

    record = manager.install(
        "https://github.com/Rudra-ravi/frida-skills/tree/main/frida-workflow"
    )

    assert record.name == "frida-workflow"
    skill_root = tmp_path / "installed" / "frida-workflow"
    assert (skill_root / "references" / "refs.md").exists()
    assert (skill_root / "scripts" / "check.py").exists()
    assert not (skill_root / ".gitignore").exists(), "repo metadata must be dropped"
    assert not list(tmp_path.joinpath("installed").glob("jobagent-skill-gh-*"))


def test_install_github_rejects_forbidden_suffix_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    manager = SkillManager(tmp_path / "installed", built_in_dir=tmp_path / "none")
    archive = _make_github_zip(
        {
            "repo-main/frida-workflow/SKILL.md": SKILL.replace(
                "interview-helper", "frida-workflow"
            ).encode(),
            "repo-main/frida-workflow/scripts/payload.exe": b"MZ\x90\x00",
        }
    )
    monkeypatch.setattr(
        SkillManager, "_download_github_archive", lambda self, o, r, b: archive
    )

    with pytest.raises(ValueError, match="forbidden file type"):
        manager.install(
            "https://github.com/Rudra-ravi/frida-skills/tree/main/frida-workflow"
        )

    assert not (tmp_path / "installed" / "frida-workflow").exists(), "install is atomic"
    assert not list(tmp_path.joinpath("installed").glob("jobagent-skill-gh-*"))
