"""Root-agent OCR tool: extract text from local images via Tesseract."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.interview.ocr import TesseractOcrExtractor

#: Formats the tool accepts. webp is converted to png via Pillow first:
#: Tesseract's bundled Leptonica on Windows commonly cannot decode webp,
#: which is exactly when the agent used to hand-roll converter scripts.
_SUPPORTED_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
)


class ImageTextFile(BaseModel):
    file_path: str = Field(
        description="图片文件路径（png/jpg/jpeg/webp/bmp/tiff），相对 JobAgent 工作区或绝对路径。",
    )


def build_read_image_text_tool(
    extractor: TesseractOcrExtractor,
    workspace_root: Path,
) -> BaseTool:
    """Expose local-image OCR as a read-only root Tool.

    Mirrors the workspace containment rules of the read_user_document family:
    one explicitly named image inside the workspace, no directory enumeration.
    """

    root = workspace_root.expanduser().resolve()

    def _resolve(file_path: str) -> Path:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("image is outside the JobAgent workspace")
        return resolved
    def _webp_to_png(image: Path) -> Path:
        # Pillow is an optional runtime dep; the import failure degrades into
        # a tool-level message instead of crashing the agent turn.
        from PIL import Image  # noqa: PLC0415 - optional dependency, lazy on purpose

        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        handle.close()
        converted = Path(handle.name)
        try:
            with Image.open(image) as picture:
                picture.save(converted, format="PNG")
        except BaseException:
            converted.unlink(missing_ok=True)
            raise
        return converted


    async def read_image_text(file_path: str) -> dict[str, Any]:
        """OCR one local image (JD screenshot, chat screenshot) to text.

        Only call with a user-provided image path; never guess filenames or
        enumerate directories. Read-only: nothing is sent anywhere.
        """

        try:
            image = _resolve(file_path)
        except ValueError as exc:
            return {"status": "failed", "error_type": "outside_workspace", "message": str(exc)}
        if not image.is_file():
            return {
                "status": "failed",
                "error_type": "not_found",
                "message": f"image not found: {image}",
            }
        if image.suffix.lower() not in _SUPPORTED_SUFFIXES:
            return {
                "status": "failed",
                "error_type": "unsupported_format",
                "message": (
                    f"仅支持图片（{' '.join(sorted(_SUPPORTED_SUFFIXES))}）；"
                    "文本文件请用 read_user_document / read_job_description。"
                ),
            }
        temporary: Path | None = None
        try:
            source = image
            if image.suffix.lower() == ".webp":
                try:
                    temporary = await asyncio.to_thread(_webp_to_png, image)
                except (ImportError, OSError, ValueError):
                    return {
                        "status": "failed",
                        "error_type": "webp_unsupported",
                        "message": "本机 Pillow 不可用或 webp 损坏，无法转换；请提供 png/jpg 版本。",
                    }
                source = temporary
            # Tesseract is a blocking subprocess; keep it off the event loop.
            extraction = await asyncio.to_thread(extractor.extract, source)
        except FileNotFoundError as exc:
            return {
                "status": "failed",
                "error_type": "ocr_unavailable",
                "message": f"本机 Tesseract OCR 不可用：{exc}",
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "failed",
                "error_type": "ocr_timeout",
                "message": f"OCR 超时（{exc.timeout}s）：图片过大或引擎卡死，请确认后重试。",
            }
        except RuntimeError as exc:
            return {"status": "failed", "error_type": "ocr_failed", "message": str(exc)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {
            "status": "completed",
            "text": extraction.text,
            "confidence": extraction.confidence,
            "engine": extraction.engine,
            "engine_version": extraction.engine_version,
            "language": extraction.language,
            "source_path": str(image),
        }

    return StructuredTool.from_function(
        coroutine=read_image_text,
        name="read_image_text",
        description=(
            "对一张本地图片做 OCR 文字提取（JD 截图、聊天截图等；支持 "
            "png/jpg/jpeg/webp/bmp/tiff，webp 自动转 png）。当前模型不支持直接读图："
            "需要图片内容时调用本工具，不要用 read_file 读图片字节或反复重试，"
            "也不要用 execute 安装 OCR 依赖或编写转换脚本。只读操作，不外发。"
        ),
        args_schema=ImageTextFile,
    )
