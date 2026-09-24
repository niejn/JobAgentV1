"""HTTP entrypoint for the JobAgent Journey-first web application."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from jobagent.agent import build_job_agent
from jobagent.config import get_settings
from jobagent.interview.ocr import TesseractOcrExtractor
from jobagent.journey import SQLiteJourneyStore
from jobagent.journey import management as journey_management
from jobagent.journey.creation import CreateJourneyRequest
from jobagent.journey.creation import create_journey as create_shared_journey
from jobagent.journey.draft import JourneyDraft, draft_response
from jobagent.matcher import LLMMatcher
from jobagent.models import Job, JobSource, Profile
from jobagent.profile import SQLiteCandidateProfileStore
from jobagent.prompts.web_journey import JOURNEY_AGENT_PROMPT

logger = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parent
UI_DIST = ROOT / "ui" / "dist"
STATE_DB = get_settings().jobagent_state_db.expanduser().resolve()


class JourneyCreate(CreateJourneyRequest):
    """The web confirmation button submits the same contract as the CLI tool."""


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", max_length=120)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)


class ConversationDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class JourneyDraftMatch(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    location: str = ""


class JourneyDraftExtract(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    current: JourneyDraft | None = None


def _journey_json(journey: Any, store: SQLiteJourneyStore) -> dict[str, Any]:
    tasks = store.list_tasks(journey.id)
    artifacts = store.list_artifacts(journey.id)
    return {
        "id": journey.id,
        "company": journey.company,
        "role": journey.role,
        "department": journey.department,
        "recruiting_cycle": journey.recruiting_cycle,
        "job_description": journey.job_description,
        "stage": journey.stage,
        "version": journey.version,
        "deleted_at": journey.deleted_at,
        "task_count": len(tasks),
        "artifact_count": len(artifacts),
        "created_at": journey.created_at.isoformat(),
        "updated_at": journey.updated_at.isoformat(),
    }


app = FastAPI(title="JobAgent Journey API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _chat_db() -> sqlite3.Connection:
    connection = sqlite3.connect(STATE_DB)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS web_conversations (
        id TEXT PRIMARY KEY, journey_id TEXT NOT NULL, title TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS web_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS assistant_conversations (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS assistant_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS office_ai_conversations (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS office_ai_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.commit()
    return connection


def _conversation_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "journey_id": row["journey_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def _chat_conversation_json(row: sqlite3.Row) -> dict[str, Any]:
    """Standalone chats (assistant / office-ai) share the same summary shape."""

    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _new_conversation_row(title: str) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "id": str(uuid4()),
        "title": title[:120] or "新对话",
        "created_at": now,
        "updated_at": now,
    }


def _list_chat_conversations(
    conversations_table: str, messages_table: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    # Table names are code-owned constants; the parameters are bound values.
    query = f"""
            SELECT c.*,
                   (SELECT m.content FROM {messages_table} m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_message
            FROM {conversations_table} c
            ORDER BY c.updated_at DESC LIMIT ? OFFSET ?
            """
    with _chat_db() as connection:
        rows = connection.execute(query, (limit, offset)).fetchall()
        conversations = []
        for row in rows:
            item = _chat_conversation_json(row)
            message = (row["last_message"] or "").strip()
            conversations.append({**item, "last_message": " ".join(message.split())[:120]})
        return conversations


async def _reply_from_web_agent(
    content: str,
    *,
    session_id: str,
    system_prompt_override: str | None = None,
    tools: list[Any] | None = None,
) -> str:
    """Run a web chat turn without leaking backend errors or resources."""

    agent = None
    try:
        agent = build_job_agent(
            get_settings(),
            tools=tools,
            platform_hint="web",
            system_prompt_override=system_prompt_override,
        )
        return await agent.reply(content, session_id=session_id)
    except Exception:  # noqa: BLE001 - browser receives a safe, stable message
        logger.exception("web agent reply failed for session %s", session_id)
        return "Agent 暂时无法回答，请稍后再试。"
    finally:
        if agent is not None:
            try:
                await agent.close()
            except Exception:  # noqa: BLE001 - cleanup must not replace the reply
                logger.exception("web agent cleanup failed for session %s", session_id)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/profile")
def get_profile() -> dict[str, Any]:
    """Return the persisted candidate identity for the workspace shell."""

    with SQLiteCandidateProfileStore(STATE_DB) as store:
        context = store.load_context()
    background = context.background if context else None
    return {
        "name": background.name if background else None,
        "email": background.email if background else None,
        "years_experience": background.years_experience if background else None,
        "skills": background.skills if background else [],
        "source": "candidate_background" if background else "empty",
    }


@app.get("/api/assistant/conversations")
def list_assistant_conversations(
    limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)
) -> list[dict[str, Any]]:
    return _list_chat_conversations(
        "assistant_conversations", "assistant_messages", limit, offset
    )


@app.post("/api/assistant/conversations", status_code=201)
def create_assistant_conversation(request: ConversationCreate) -> dict[str, Any]:
    conversation = _new_conversation_row(request.title)
    with _chat_db() as connection:
        connection.execute(
            "INSERT INTO assistant_conversations VALUES (?, ?, ?, ?)",
            tuple(conversation.values()),
        )
    return conversation


@app.get("/api/assistant/conversations/{conversation_id}")
def get_assistant_conversation(conversation_id: str) -> dict[str, Any]:
    with _chat_db() as connection:
        conversation = connection.execute(
            "SELECT * FROM assistant_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Assistant conversation not found")
        messages = connection.execute(
            "SELECT * FROM assistant_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return {
            **_chat_conversation_json(conversation),
            "messages": [_message_json(row) for row in messages],
        }


@app.post("/api/assistant/conversations/{conversation_id}/messages")
async def send_assistant_message(
    conversation_id: str, request: MessageCreate
) -> dict[str, Any]:
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    with _chat_db() as connection:
        conversation = connection.execute(
            "SELECT * FROM assistant_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Assistant conversation not found")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO assistant_messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)",
            (conversation_id, content, now),
        )
    response = await _reply_from_web_agent(
        content, session_id=f"assistant:{conversation_id}"
    )
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _chat_db() as connection:
        connection.execute(
            "INSERT INTO assistant_messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'assistant', ?, ?)",
            (conversation_id, response, now),
        )
        connection.execute(
            "UPDATE assistant_conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    return {"role": "assistant", "content": response, "created_at": now}


@app.delete("/api/assistant/conversations")
def delete_assistant_conversations(request: ConversationDelete) -> dict[str, int]:
    """Delete user-visible JobAgent history records without touching checkpoints."""

    return _delete_chat_conversations("assistant", request.ids)


def _delete_chat_conversations(prefix: str, ids: list[str]) -> dict[str, int]:
    """Delete conversations + messages from the ``<prefix>_`` chat tables."""

    unique_ids = list(dict.fromkeys(ids))
    placeholders = ",".join("?" for _ in unique_ids)
    with _chat_db() as connection:
        connection.execute(
            f"DELETE FROM {prefix}_messages WHERE conversation_id IN ({placeholders})",
            unique_ids,
        )
        result = connection.execute(
            f"DELETE FROM {prefix}_conversations WHERE id IN ({placeholders})",
            unique_ids,
        )
    return {"deleted": result.rowcount}


@app.post("/api/office-ai/conversations", status_code=201)
def create_office_ai_conversation(request: ConversationCreate) -> dict[str, Any]:
    conversation = _new_conversation_row(request.title)
    with _chat_db() as connection:
        connection.execute(
            "INSERT INTO office_ai_conversations VALUES (?, ?, ?, ?)",
            tuple(conversation.values()),
        )
    return conversation


@app.get("/api/office-ai/conversations")
def list_office_ai_conversations(
    limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)
) -> list[dict[str, Any]]:
    return _list_chat_conversations(
        "office_ai_conversations", "office_ai_messages", limit, offset
    )


@app.get("/api/office-ai/conversations/{conversation_id}")
def get_office_ai_conversation(conversation_id: str) -> dict[str, Any]:
    with _chat_db() as connection:
        conversation = connection.execute(
            "SELECT * FROM office_ai_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Office AI conversation not found")
        messages = connection.execute(
            "SELECT * FROM office_ai_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return {
            **_chat_conversation_json(conversation),
            "messages": [_message_json(row) for row in messages],
        }


@app.post("/api/office-ai/conversations/{conversation_id}/messages")
async def send_office_ai_message(
    conversation_id: str, request: MessageCreate
) -> dict[str, Any]:
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    with _chat_db() as connection:
        conversation = connection.execute(
            "SELECT * FROM office_ai_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Office AI conversation not found")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO office_ai_messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)",
            (conversation_id, content, now),
        )
    response = await _reply_from_web_agent(
        content, session_id=f"office_ai:{conversation_id}"
    )
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _chat_db() as connection:
        connection.execute(
            "INSERT INTO office_ai_messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'assistant', ?, ?)",
            (conversation_id, response, now),
        )
        connection.execute(
            "UPDATE office_ai_conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    return {"role": "assistant", "content": response, "created_at": now}


@app.delete("/api/office-ai/conversations")
def delete_office_ai_conversations(request: ConversationDelete) -> dict[str, int]:
    """Delete user-visible JobAgent history records without touching checkpoints."""

    return _delete_chat_conversations("office_ai", request.ids)


@app.get("/api/journeys")
def list_journeys(
    company: str = "",
    include_deleted: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return journey_management.list_journeys(
        STATE_DB,
        company=company,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )


@app.post("/api/journeys", status_code=201)
def create_journey(request: JourneyCreate) -> dict[str, Any]:
    journey, created = create_shared_journey(STATE_DB, request)
    with SQLiteJourneyStore(STATE_DB) as store:
        return {**_journey_json(journey, store), "created": created}


@app.post("/api/journey-drafts/match")
async def match_journey_draft(request: JourneyDraftMatch) -> dict[str, Any]:
    """Return a preliminary, non-binding fit opinion before creation."""

    with SQLiteCandidateProfileStore(STATE_DB) as store:
        context = store.load_context()
    if context is None or context.background is None:
        return {
            "score": None,
            "evaluation_failed": True,
            "reasoning": ["尚未找到已确认的候选人画像"],
        }
    background = context.background
    profile = Profile(
        name=background.name,
        email=background.email,
        years_experience=background.years_experience,
        summary=background.summary,
        skills=background.skills,
        desired_roles=context.search_profile.desired_roles if context.search_profile else [],
        preferred_locations=(
            context.search_profile.preferred_locations if context.search_profile else []
        ),
    )
    job = Job(
        source=JobSource.BOSS,
        title=request.role,
        company=request.company,
        location=request.location or "未填写",
        url=HttpUrl("https://jobagent.local/journey-draft"),
        description=request.description,
    )
    try:
        result = await LLMMatcher(settings=get_settings()).match(job, profile)
        return {
            "score": round(result.score * 100),
            "evaluation_failed": result.evaluation_failed,
            "reasoning": result.reasoning,
            "matched_skills": result.matched_skills,
            "missing_skills": result.missing_skills,
        }
    except Exception as error:  # preliminary feedback must not block creation
        return {
            "score": None,
            "evaluation_failed": True,
            "reasoning": [f"暂时无法完成匹配评估：{error}"],
        }


@app.post("/api/journey-drafts/extract")
def extract_journey_draft(request: JourneyDraftExtract) -> dict[str, Any]:
    if not request.text.strip():
        raise HTTPException(status_code=422, detail="请输入岗位文本")
    return draft_response(request.text, request.current)


@app.post("/api/journey-drafts/ocr")
async def ocr_journey_draft(
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Read a user-provided JD screenshot before the Journey exists."""

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="请上传 JPG、PNG 或 WEBP 图片")
    suffix = Path(file.filename or "jd-image").suffix.lower() or ".png"
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}:
        suffix = ".png"
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(payload) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片不能超过 15 MB")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(payload)
            temporary_path = Path(handle.name)
        settings = get_settings()
        extractor = TesseractOcrExtractor(
            command=settings.tesseract_cmd,
            language=settings.jobagent_ocr_language,
            page_segmentation_mode=settings.jobagent_ocr_psm,
            timeout_seconds=30,
        )
        # Tesseract is a blocking subprocess; keep it off FastAPI's event loop.
        result = await asyncio.to_thread(extractor.extract, temporary_path)
        logger.info(
            "journey JD OCR completed: filename=%s confidence=%.2f engine=%s",
            file.filename,
            result.confidence,
            result.engine,
        )
        return {
            **draft_response(result.text),
            "text": result.text,
            "confidence": result.confidence,
            "engine": result.engine,
            "language": result.language,
            "filename": file.filename or "jd-image",
        }
    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail="本机未安装 Tesseract OCR") from error
    except RuntimeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@app.get("/api/journeys/{journey_id}")
def get_journey(journey_id: str) -> dict[str, Any]:
    try:
        return journey_management.get_journey(STATE_DB, journey_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Journey not found") from error


def _manage_journey(
    journey_id: str, action: str, request: journey_management.JourneyTarget
) -> dict[str, Any]:
    if journey_id != request.journey_id:
        raise HTTPException(status_code=422, detail="Journey ID mismatch")
    try:
        return journey_management.change_journey(STATE_DB, action, request)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Journey not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.patch("/api/journeys/{journey_id}")
def update_journey(
    journey_id: str, request: journey_management.JourneyUpdate
) -> dict[str, Any]:
    return _manage_journey(journey_id, "update", request)


@app.delete("/api/journeys/{journey_id}")
def delete_journey(
    journey_id: str, request: journey_management.JourneyTarget
) -> dict[str, Any]:
    return _manage_journey(journey_id, "delete", request)


@app.post("/api/journeys/{journey_id}/restore")
def restore_journey(
    journey_id: str, request: journey_management.JourneyTarget
) -> dict[str, Any]:
    return _manage_journey(journey_id, "restore", request)


@app.get("/api/journeys/{journey_id}/conversations")
def list_conversations(journey_id: str) -> list[dict[str, Any]]:
    with SQLiteJourneyStore(STATE_DB) as store:
        try:
            store.get_journey(journey_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Journey not found") from error
    with _chat_db() as connection:
        rows = connection.execute(
            "SELECT * FROM web_conversations WHERE journey_id = ? ORDER BY updated_at DESC",
            (journey_id,),
        ).fetchall()
        return [_conversation_json(row) for row in rows]


@app.post("/api/journeys/{journey_id}/conversations", status_code=201)
def create_conversation(journey_id: str, request: ConversationCreate) -> dict[str, Any]:
    with SQLiteJourneyStore(STATE_DB) as store:
        try:
            store.get_journey(journey_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Journey not found") from error
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conversation = {
        "id": str(uuid4()),
        "journey_id": journey_id,
        "title": request.title[:120] or "新对话",
        "created_at": now,
        "updated_at": now,
    }
    with _chat_db() as connection:
        connection.execute(
            "INSERT INTO web_conversations VALUES (?, ?, ?, ?, ?)",
            tuple(conversation.values()),
        )
    return conversation


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    with _chat_db() as connection:
        conversation = connection.execute(
            "SELECT * FROM web_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = connection.execute(
            "SELECT * FROM web_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return {
            **_conversation_json(conversation),
            "messages": [_message_json(row) for row in messages],
        }


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, request: MessageCreate) -> dict[str, Any]:
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    with _chat_db() as connection:
        conversation = connection.execute(
            "SELECT * FROM web_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO web_messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)",
            (conversation_id, content, now),
        )
    journey_prompt: str | None = None
    try:
        with SQLiteJourneyStore(STATE_DB) as journey_store:
            journey = journey_store.get_journey(str(conversation["journey_id"]))
        journey_prompt = JOURNEY_AGENT_PROMPT.format(
            company=journey.company,
            role=journey.role,
            description=journey.job_description,
        )
    except Exception:  # a broken journey reference must not 500 the chat
        logger.exception("journey lookup failed for conversation %s", conversation_id)
    if journey_prompt is None:
        response = "Agent 暂时无法回答，请稍后再试。"
    else:
        response = await _reply_from_web_agent(
            content,
            session_id=conversation_id,
            system_prompt_override=journey_prompt,
            tools=[],
        )
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _chat_db() as connection:
        connection.execute(
            "INSERT INTO web_messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'assistant', ?, ?)",
            (conversation_id, response, now),
        )
        connection.execute(
            "UPDATE web_conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    return {"role": "assistant", "content": response, "created_at": now}


class UploadedResumeResponse(BaseModel):
    saved_as: str
    size_bytes: int


def _safe_skill_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    return base


RESUME_SUFFIXES = {".pdf", ".docx", ".doc", ".md", ".txt"}
RESUME_MAX_BYTES = 20 * 1024 * 1024


@app.get("/api/resumes")
def list_resumes() -> list[dict[str, Any]]:
    """List resume files available to the sidebar and agent."""
    directory = _safe_skill_dir(ROOT.parent / "data" / "resumes")
    items = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if entry.is_file() and entry.suffix.lower() in RESUME_SUFFIXES:
            stat = entry.stat()
            items.append({
                "name": entry.name,
                "size_bytes": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime)
                .astimezone()
                .isoformat(timespec="seconds"),
            })
    return items


@app.post("/api/resumes/upload", status_code=201)
async def upload_resume(
    file: Annotated[UploadFile, File()],
) -> UploadedResumeResponse:
    """Store an uploaded resume file under data/resumes/."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in RESUME_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的简历格式：{suffix or '未知'}（支持 pdf/docx/doc/md/txt）",
        )
    directory = _safe_skill_dir(ROOT.parent / "data" / "resumes")
    target = directory / Path(file.filename or "resume").name
    size = 0
    with target.open("wb") as sink:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > RESUME_MAX_BYTES:
                sink.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="简历文件不能超过 20 MB")
            sink.write(chunk)
    return UploadedResumeResponse(saved_as=target.name, size_bytes=size)


@app.get("/api/app/version")
def app_version() -> dict[str, Any]:
    """Version metadata for the settings panel and the (placeholder) updater."""
    return {"product": "JobAgent", "version": "0.1.0", "updater": "placeholder", "channel": "dev"}


if UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
