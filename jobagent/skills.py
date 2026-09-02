"""Discovery and installation of standard, documentation-only Skills.

A Skill is a directory containing a ``SKILL.md`` with YAML frontmatter. Skill
content is never imported as Python: the Agent reads the markdown and follows
its instructions with its already-registered tools.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path(__file__).parent.parent.resolve() / "skills"
DEFAULT_INSTALLED_SKILLS_DIR = Path("data/skills")
MAX_SKILL_BYTES = 512_000
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillMetadata(BaseModel):
    """Frontmatter fields parsed from SKILL.md."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    author: str | None = None
    version: str | None = None


class SkillRecord(SkillMetadata):
    """Metadata plus the source path used by the Agent."""

    skill_name: str
    path: Path
    installed: bool = False


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


class SkillManager:
    """Manage built-in and user-installed documentation Skills safely."""

    def __init__(
        self,
        installed_dir: Path = DEFAULT_INSTALLED_SKILLS_DIR,
        *,
        built_in_dir: Path = DEFAULT_SKILLS_DIR,
    ) -> None:
        self.installed_dir = installed_dir.expanduser().resolve()
        self.built_in_dir = built_in_dir.expanduser().resolve()

    def list_skills(self) -> list[SkillRecord]:
        records: dict[str, SkillRecord] = {}
        for root, installed in ((self.built_in_dir, False), (self.installed_dir, True)):
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                skill_md = entry / "SKILL.md"
                if not entry.is_dir() or not skill_md.is_file():
                    continue
                metadata = _parse_skill_metadata(skill_md)
                if metadata is None:
                    continue
                records[metadata.name] = SkillRecord(
                    **metadata.model_dump(),
                    skill_name=entry.name,
                    path=skill_md,
                    installed=installed,
                )
        return sorted(records.values(), key=lambda record: record.name.lower())

    def read_skill(self, name: str) -> str:
        """Read one discovered Skill by directory or metadata name."""

        record = next(
            (item for item in self.list_skills() if item.skill_name == name or item.name == name),
            None,
        )
        if record is None:
            raise ValueError(f"skill not found: {name}")
        return record.path.read_text(encoding="utf-8")

    def install(self, source: str) -> SkillRecord:
        """Install a local directory/file or HTTPS SKILL.md into the user directory."""

        source = source.strip()
        if not source:
            raise ValueError("skill source is required")
        source_path = Path(source).expanduser().resolve()
        if source_path.is_dir() or source_path.is_file():
            skill_md = source_path / "SKILL.md" if source_path.is_dir() else source_path
            if not skill_md.is_file():
                raise ValueError(
                    "source must be a directory containing SKILL.md or a SKILL.md file"
                )
            if skill_md.stat().st_size > MAX_SKILL_BYTES:
                raise ValueError("SKILL.md exceeds the size limit")
            return self._install_text(skill_md.read_text(encoding="utf-8"))
        if urlsplit(source).scheme:
            skill_text = self._download(source)
            return self._install_text(skill_text)
        raise ValueError("source must be a local Skill path or an HTTPS URL")

    @staticmethod
    def _download(source: str) -> str:
        parsed = urlsplit(source)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Skill URLs must use HTTPS")
        try:
            with httpx.Client(follow_redirects=True, timeout=20.0) as client:
                response = client.get(source)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ValueError(f"failed to download Skill: {exc}") from exc
        if urlsplit(str(response.url)).scheme != "https":
            raise ValueError("Skill download redirected away from HTTPS")
        if len(response.content) > MAX_SKILL_BYTES:
            raise ValueError("downloaded SKILL.md exceeds the size limit")
        return response.content.decode("utf-8")

    def _install_text(self, content: str) -> SkillRecord:
        with tempfile.TemporaryDirectory(prefix="jobagent-skill-") as temp_dir:
            temp_path = Path(temp_dir) / "SKILL.md"
            temp_path.write_text(content, encoding="utf-8")
            metadata = _parse_skill_metadata(temp_path)
            if metadata is None:
                raise ValueError(
                    "SKILL.md must contain valid YAML frontmatter with name and description"
                )
            directory_name = metadata.name.strip()
            if not _SAFE_NAME.fullmatch(directory_name):
                raise ValueError(
                    "skill name must contain only letters, numbers, dots, underscores, or hyphens"
                )
            target = (self.installed_dir / directory_name).resolve()
            if not target.parent == self.installed_dir:
                raise ValueError("invalid skill installation path")
            self.installed_dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{directory_name}-", dir=self.installed_dir))
            try:
                shutil.copy2(temp_path, staging / "SKILL.md")
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        return next(item for item in self.list_skills() if item.name == metadata.name)
