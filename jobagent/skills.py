"""Discovery and installation of standard Skills.

A Skill is a directory containing a ``SKILL.md`` with YAML frontmatter, plus
optional ``scripts/`` / ``references/`` / ``assets/`` payloads referenced by
the markdown. Skill content is never imported as Python: the Agent reads the
markdown and follows its instructions with its already-registered tools
(bundled scripts run through the ``execute`` tool's approval path, not us).
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path(__file__).parent.parent.resolve() / "skills"
DEFAULT_INSTALLED_SKILLS_DIR = Path("data/skills")
MAX_SKILL_BYTES = 512_000
MAX_SKILL_DIR_BYTES = 5_000_000
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Reject only opaque/unsafe binaries a human cannot audit by reading them.
# Plain-text scripts (.sh/.ps1/.py/…) are allowed: they are auditable and the
# real execution boundary is the `execute` tool's approval path, not file type.
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".exe", ".dll", ".sys", ".com", ".msi", ".cpl", ".msc", ".scr",
        ".lnk", ".jar", ".so", ".dylib", ".bin", ".run", ".app",
    }
)

MAX_ARCHIVE_BYTES = 50_000_000


def _parse_github_url(source: str) -> tuple[str, str, str, str] | None:
    """Parse a GitHub repository URL into ``(owner, repo, branch, subpath)``.

    Accepts ``https://github.com/{owner}/{repo}`` and
    ``https://github.com/{owner}/{repo}/tree/{branch}[/{subpath}]``.
    Returns ``None`` for any non-GitHub URL.
    """

    parsed = urlsplit(source)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        return None
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    branch, subpath = "main", ""
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
        subpath = "/".join(parts[4:])
    return owner, repo, branch, subpath


def _validate_skill_tree(root: Path) -> list[Path]:
    """Validate a skill directory tree and return safe relative file paths.

    Rejects symlinks, hidden entries, forbidden executable suffixes, and
    trees exceeding ``MAX_SKILL_DIR_BYTES``. Only regular files and plain
    subdirectories may ship with a Skill.
    """

    skill_md = root / "SKILL.md"
    if not skill_md.is_file() or skill_md.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError("skill directory must contain a SKILL.md within the size limit")

    files: list[Path] = [Path("SKILL.md")]
    total = skill_md.stat().st_size
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            if entry.name.startswith("."):
                raise ValueError(f"hidden entries are not allowed: {entry.name}")
            if entry.is_symlink():
                raise ValueError(f"symlinks are not allowed: {entry.name}")
            if entry.is_dir():
                stack.append(entry)
                continue
            if not entry.is_file():
                raise ValueError(f"non-regular files are not allowed: {entry.name}")
            if entry.suffix.lower() in _FORBIDDEN_SUFFIXES:
                raise ValueError(f"forbidden file type: {entry.name}")
            size = entry.stat().st_size
            total += size
            if total > MAX_SKILL_DIR_BYTES:
                raise ValueError("skill directory exceeds the total size limit")
            files.append(entry.relative_to(root))
    return files


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
    """Manage built-in and user-installed Skills (markdown plus bundled files) safely."""

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

    def find_skill(self, name: str) -> SkillRecord | None:
        """Find one discovered Skill by directory or metadata name."""

        return next(
            (item for item in self.list_skills() if item.skill_name == name or item.name == name),
            None,
        )

    def read_skill(self, name: str) -> str:
        """Read one discovered Skill by directory or metadata name."""

        record = self.find_skill(name)
        if record is None:
            raise ValueError(f"skill not found: {name}")
        return record.path.read_text(encoding="utf-8")

    @staticmethod
    def list_skill_files(record: SkillRecord) -> list[str]:
        """List every file of a skill relative to its directory."""

        root = record.path.parent
        return sorted(
            entry.relative_to(root).as_posix() for entry in root.rglob("*") if entry.is_file()
        )

    def install(self, source: str) -> SkillRecord:
        """Install a Skill from a local directory/file or an HTTPS URL.

        Directory sources install the whole validated tree (SKILL.md plus
        scripts/, references/, assets/…); GitHub repository URLs install a
        skill from the repo root or a ``/tree/<branch>/<subpath>`` location;
        plain HTTPS SKILL.md URLs install markdown only.
        """

        source = source.strip()
        if not source:
            raise ValueError("skill source is required")
        source_path = Path(source).expanduser().resolve()
        if source_path.is_dir():
            return self._install_tree(source_path)
        if source_path.is_file():
            if source_path.stat().st_size > MAX_SKILL_BYTES:
                raise ValueError("SKILL.md exceeds the size limit")
            return self._install_text(source_path.read_text(encoding="utf-8"))
        if urlsplit(source).scheme:
            if _parse_github_url(source) is not None:
                return self._install_github(source)
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

    def _install_github(self, source: str) -> SkillRecord:
        """Install a skill from a GitHub repository (root or subpath)."""

        parsed = _parse_github_url(source)
        if parsed is None:
            raise ValueError("not a GitHub repository URL")
        owner, repo, branch, subpath = parsed
        archive = self._download_github_archive(owner, repo, branch)
        staging_extract = Path(tempfile.mkdtemp(prefix="jobagent-skill-gh-"))
        try:
            skill_dir = self._extract_skill_from_archive(archive, subpath, staging_extract)
            return self._install_tree(skill_dir)
        finally:
            shutil.rmtree(staging_extract, ignore_errors=True)

    def _download_github_archive(self, owner: str, repo: str, branch: str) -> bytes:
        urls = [
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}",
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/tags/{branch}",
        ]
        if branch == "main":
            urls.insert(1, f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/master")
        last_error = "no attempt made"
        for url in urls:
            try:
                with httpx.Client(follow_redirects=True, timeout=60.0) as client:
                    response = client.get(url)
                    response.raise_for_status()
                if len(response.content) > MAX_ARCHIVE_BYTES:
                    raise ValueError("downloaded archive exceeds the size limit")
                return response.content
            except httpx.HTTPError as exc:
                last_error = str(exc)
        raise ValueError(
            f"failed to download GitHub archive for {owner}/{repo}@{branch}: {last_error}"
        )

    @staticmethod
    def _extract_skill_from_archive(data: bytes, subpath: str, extract_root: Path) -> Path:
        """Extract a GitHub zip into ``extract_root`` and locate the skill dir.

        Strips the repository-root wrapper segment, skips hidden entries
        (``.gitignore`` and friends), enforces the archive size limit, and
        rejects path traversal. Returns the directory containing SKILL.md.
        """

        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("downloaded archive is not a valid zip") from exc
        names = archive.namelist()
        if not names:
            raise ValueError("downloaded archive is empty")
        root_prefix = names[0].split("/", 1)[0]

        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if len(parts) < 2 or parts[0] != root_prefix:
                continue
            rel_parts = parts[1:]
            if any(part == ".." for part in rel_parts):
                raise ValueError(f"unsafe archive entry: {info.filename}")
            if any(part.startswith(".") for part in rel_parts):
                continue
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("downloaded archive exceeds the size limit")
            destination = extract_root.joinpath(*rel_parts)
            if not destination.resolve().is_relative_to(extract_root.resolve()):
                raise ValueError(f"unsafe archive entry: {info.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source_file, open(destination, "wb") as target_file:
                shutil.copyfileobj(source_file, target_file)

        skill_dir = extract_root / subpath if subpath else extract_root
        if not (skill_dir / "SKILL.md").is_file():
            candidates = sorted(
                entry.relative_to(extract_root).as_posix()
                for entry in extract_root.rglob("SKILL.md")
                if entry.is_file()
            )
            found = "; ".join(candidates[:10]) if candidates else "none found"
            raise ValueError(
                f"no SKILL.md at '{subpath or '.'}' in the repository (candidates: {found})"
            )
        return skill_dir

    def _resolve_target(self, name: str) -> tuple[str, Path]:
        directory_name = name.strip()
        if not _SAFE_NAME.fullmatch(directory_name):
            raise ValueError(
                "skill name must contain only letters, numbers, dots, underscores, or hyphens"
            )
        target = (self.installed_dir / directory_name).resolve()
        if not target.parent == self.installed_dir:
            raise ValueError("invalid skill installation path")
        return directory_name, target

    @staticmethod
    def _swap(staging: Path, target: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)

    def _record_for(self, name: str) -> SkillRecord:
        return next(item for item in self.list_skills() if item.name == name)

    def _install_text(self, content: str) -> SkillRecord:
        with tempfile.TemporaryDirectory(prefix="jobagent-skill-") as temp_dir:
            temp_path = Path(temp_dir) / "SKILL.md"
            temp_path.write_text(content, encoding="utf-8")
            metadata = _parse_skill_metadata(temp_path)
            if metadata is None:
                raise ValueError(
                    "SKILL.md must contain valid YAML frontmatter with name and description"
                )
            directory_name, target = self._resolve_target(metadata.name)
            self.installed_dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{directory_name}-", dir=self.installed_dir))
            try:
                shutil.copy2(temp_path, staging / "SKILL.md")
                self._swap(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        return self._record_for(metadata.name)

    def _install_tree(self, source_dir: Path) -> SkillRecord:
        """Install a validated skill directory tree atomically."""

        files = _validate_skill_tree(source_dir)
        metadata = _parse_skill_metadata(source_dir / "SKILL.md")
        if metadata is None:
            raise ValueError(
                "SKILL.md must contain valid YAML frontmatter with name and description"
            )
        directory_name, target = self._resolve_target(metadata.name)
        self.installed_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{directory_name}-", dir=self.installed_dir))
        try:
            for relative in files:
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_dir / relative, destination)
            self._swap(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return self._record_for(metadata.name)
