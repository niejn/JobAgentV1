"""Traceable OCR extraction for downloaded Source Images."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class ImageContentExtraction:
    source_image: Path
    source_hash: str
    text: str
    confidence: float
    engine: str
    engine_version: str
    language: str


class TesseractOcrExtractor:
    """Extract text through the local Tesseract CLI and retain provenance."""

    def __init__(
        self,
        *,
        command: str | Path = "tesseract",
        language: str = "chi_sim+eng",
        page_segmentation_mode: int = 11,
        timeout_seconds: int = 120,
        runner: Runner | None = None,
    ) -> None:
        self._command = str(command)
        self._language = language
        self._psm = page_segmentation_mode
        self._timeout = timeout_seconds
        self._runner = runner or self._run
        self._version: str | None = None

    def extract(self, image_path: Path) -> ImageContentExtraction:
        image = image_path.expanduser().resolve()
        if not image.is_file():
            raise FileNotFoundError(f"Source Image not found: {image}")
        command = [
            self._command,
            str(image),
            "stdout",
            "-l",
            self._language,
            "--psm",
            str(self._psm),
            "tsv",
        ]
        completed = self._runner(command)
        if completed.returncode != 0:
            message = completed.stderr.strip() or "unknown OCR error"
            raise RuntimeError(f"Tesseract OCR failed: {message}")
        text, confidence = _parse_tsv(completed.stdout)
        return ImageContentExtraction(
            source_image=image,
            source_hash=_sha256(image),
            text=text,
            confidence=confidence,
            engine="tesseract",
            engine_version=self._engine_version(),
            language=self._language,
        )

    def _engine_version(self) -> str:
        if self._version is None:
            completed = self._runner([self._command, "--version"])
            if completed.returncode != 0:
                self._version = "unknown"
            else:
                match = re.search(r"tesseract\s+([^\s]+)", completed.stdout, re.IGNORECASE)
                self._version = match.group(1) if match else "unknown"
        return self._version

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout,
        )


def _parse_tsv(payload: str) -> tuple[str, float]:
    reader = csv.DictReader(io.StringIO(payload), delimiter="\t")
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    confidences: list[float] = []
    for row in reader:
        word = (row.get("text") or "").strip()
        if not word:
            continue
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        if confidence < 0:
            continue
        key = (
            row.get("page_num") or "0",
            row.get("block_num") or "0",
            row.get("par_num") or "0",
            row.get("line_num") or "0",
        )
        lines.setdefault(key, []).append(word)
        confidences.append(confidence)
    text = "\n".join(" ".join(words) for words in lines.values())
    average = sum(confidences) / len(confidences) / 100 if confidences else 0.0
    return text, round(average, 4)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
