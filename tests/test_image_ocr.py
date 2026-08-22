"""Behavior tests for Source Image content extraction."""

from pathlib import Path
from subprocess import CompletedProcess

from jobagent.interview.ocr import TesseractOcrExtractor


def test_tesseract_extractor_returns_traceable_text_and_confidence(tmp_path: Path) -> None:
    image = tmp_path / "image_0.jpg"
    image.write_bytes(b"fake-image")
    tsv = "\t".join(
        (
            "level",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        )
    )
    tsv += "\n5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90.0\t字节后端\n"
    tsv += "5\t1\t1\t1\t1\t2\t1\t0\t1\t1\t80.0\t一面面经\n"

    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        if "--version" in command:
            return CompletedProcess(command, 0, "tesseract 5.4.0", "")
        return CompletedProcess(command, 0, tsv, "")

    result = TesseractOcrExtractor(command="tesseract", runner=fake_runner).extract(image)

    assert result.text == "字节后端 一面面经"
    assert result.confidence == 0.85
    assert result.engine == "tesseract"
    assert result.engine_version == "5.4.0"
    assert result.source_image == image.resolve()
    assert result.source_hash.startswith("sha256:")
