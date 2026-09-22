"""read_image_text: root-agent OCR over local images via Tesseract."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobagent.interview.ocr import TesseractOcrExtractor
from jobagent.tools.image_text import build_read_image_text_tool

_TSV_OK = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t92.5\tGoodNotes\n"
    "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t88.0\t岗位\n"
)


def _fake_extractor(responses: list[str] | None = None, error: Exception | None = None):
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if error is not None:
            raise error
        payload = responses[0] if responses else _TSV_OK
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    return TesseractOcrExtractor(runner=runner), calls


def _tiny_image(directory: Path, name: str) -> Path:
    from PIL import Image

    path = directory / name
    Image.new("RGB", (4, 4), color=(255, 255, 255)).save(path)
    return path


@pytest.mark.asyncio
async def test_returns_text_with_provenance_for_png(tmp_path: Path) -> None:
    extractor, _ = _fake_extractor()
    _tiny_image(tmp_path, "jd.png")
    tool = build_read_image_text_tool(extractor, tmp_path)

    result = await tool.ainvoke({"file_path": "jd.png"})

    assert result["status"] == "completed"
    assert result["text"] == "GoodNotes 岗位"
    assert result["confidence"] == pytest.approx(0.9025)
    assert result["engine"] == "tesseract"
    assert result["source_path"].endswith("jd.png")


@pytest.mark.asyncio
async def test_webp_is_converted_to_png_before_ocr(tmp_path: Path) -> None:
    extractor, calls = _fake_extractor()
    image = _tiny_image(tmp_path, "jd.webp")
    tool = build_read_image_text_tool(extractor, tmp_path)

    result = await tool.ainvoke({"file_path": str(image)})

    assert result["status"] == "completed"
    ocr_input = Path(calls[0][1])
    assert ocr_input.suffix == ".png", "webp must be converted before Tesseract"
    assert not ocr_input.exists(), "temporary png must be cleaned up"


@pytest.mark.asyncio
async def test_rejects_non_image_files_with_pointer_to_text_tool(tmp_path: Path) -> None:
    extractor, calls = _fake_extractor()
    (tmp_path / "jd.txt").write_text("岗位", encoding="utf-8")
    tool = build_read_image_text_tool(extractor, tmp_path)

    result = await tool.ainvoke({"file_path": "jd.txt"})

    assert result["status"] == "failed"
    assert result["error_type"] == "unsupported_format"
    assert "read_user_document" in result["message"]
    assert calls == [], "no OCR subprocess may run for unsupported formats"


@pytest.mark.asyncio
async def test_workspace_containment(tmp_path: Path) -> None:
    extractor, _ = _fake_extractor()
    tool = build_read_image_text_tool(extractor, tmp_path)

    result = await tool.ainvoke({"file_path": str(tmp_path.parent / "elsewhere.png")})

    assert result["status"] == "failed"
    assert result["error_type"] in {"outside_workspace", "not_found"}


@pytest.mark.asyncio
async def test_missing_tesseract_degrades_to_message(tmp_path: Path) -> None:
    extractor, _ = _fake_extractor(error=FileNotFoundError("tesseract not found"))
    _tiny_image(tmp_path, "jd.png")
    tool = build_read_image_text_tool(extractor, tmp_path)

    result = await tool.ainvoke({"file_path": "jd.png"})

    assert result["status"] == "failed"
    assert result["error_type"] == "ocr_unavailable"
