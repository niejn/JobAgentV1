"""Offline reliability checks, including actual independent spawned processes."""

import asyncio
import json
import multiprocessing
import os
import sqlite3
import threading

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from jobagent.agent import build_job_agent
from jobagent.applier.email_sent_verifier import SentVerification
from jobagent.config import Settings
from jobagent.journey.creation import CreateJourneyRequest, create_journey
from jobagent.journey.recruitment_notes import RecruitmentNoteRegistry, extract_note
from jobagent.journey.store import SQLiteJourneyStore
from jobagent.journey.xhs_email_drafts import XhsEmailDraftService
from jobagent.tools.resume_library import ResumeLibrary
from jobagent.tools.xhs_recruitment import build_send_recruitment_email_tool


class Sender:
    def __init__(self, status="ok"):
        self.status = status
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": self.status}


class Verifier:
    def __init__(self, status="submitted"):
        self.status = status
        self.calls = []

    def find(self, message_id):
        self.calls.append(message_id)
        return SentVerification(self.status, message_id, "test_sent_lookup")


@pytest.fixture
def delivery(tmp_path):
    database = tmp_path / "state.db"
    settings = Settings(_env_file=None, jobagent_email_smtp_password="")
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(extract_note("n", "https://xhs/n", "招聘", "招聘 AI 工程师 hr@example.com", ""))
        notes.select_position(note_id="n", position_index=0, company="Example")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    sender, verifier = Sender(), Verifier()
    service = XhsEmailDraftService(database, resumes, settings, sender, verifier)
    draft = service.prepare("n", "resume.pdf", "Applicant")
    journey, _ = create_journey(
        database, CreateJourneyRequest(company="Example", role="AI 工程师", source_job_id="xhs:n")
    )
    return database, resumes, settings, service, draft["draft_id"], journey, sender, verifier


def read_delivery(database, draft_id):
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM xhs_email_deliveries WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        )


class ProcessSender:
    def __init__(self, database, draft_id, entered, release, crash):
        self.database, self.draft_id = database, draft_id
        self.entered, self.release, self.crash = entered, release, crash

    def send(self, **kwargs):
        row = read_delivery(self.database, self.draft_id)
        assert row["status"] == "sending"
        assert row["message_id"] == kwargs["message_id"]
        assert json.loads(row["receipt"])["message_id"] == kwargs["message_id"]
        self.entered.set()
        if self.crash:
            os._exit(0)  # Simulate SMTP accepted, then process died before receipt persistence.
        assert self.release.wait(15)
        return {"status": "ok"}


def process_send(database, resumes_path, draft_id, barrier, entered, release, output, crash=False):
    service = XhsEmailDraftService(
        database,
        ResumeLibrary(resumes_path),
        Settings(_env_file=None),
        ProcessSender(database, draft_id, entered, release, crash),
    )
    barrier.wait(timeout=15)
    output.put(asyncio.run(service.send(draft_id)))


def test_two_processes_only_one_enters_smtp(delivery, tmp_path):
    database, _, _, _, draft_id, *_ = delivery
    context = multiprocessing.get_context("spawn")
    barrier, entered, release = context.Barrier(2), context.Event(), context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=process_send,
            args=(database, tmp_path / "resumes", draft_id, barrier, entered, release, output),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert entered.wait(15)
        # The competing caller returns while the winner is still inside SMTP.
        assert output.get(timeout=15)["status"] == "sending"
        release.set()
        assert output.get(timeout=15)["status"] == "submitted"
    finally:
        release.set()
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
        output.close()
    assert [p.exitcode for p in processes] == [0, 0]


@pytest.mark.asyncio
async def test_concurrent_calls_and_recovery_during_smtp(delivery):
    database, resumes, settings, service, draft_id, _, _, verifier = delivery
    entered, release = threading.Event(), threading.Event()
    service._sender = ProcessSender(database, draft_id, entered, release, False)
    task = asyncio.create_task(service.send(draft_id))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        competitor = XhsEmailDraftService(database, resumes, settings, Sender(), verifier)
        assert (await competitor.send(draft_id))["status"] == "sending"
        verifier.status = "unverified"
        await asyncio.to_thread(competitor.recover)
        assert (await competitor.send(draft_id))["status"] == "unverified"
    finally:
        release.set()
    assert (await task)["status"] == "submitted"
    assert read_delivery(database, draft_id)["status"] == "submitted"


def test_process_crash_then_jobagent_startup_recovers_sent(delivery, tmp_path, monkeypatch):
    database, _, settings, service, draft_id, journey, sender, verifier = delivery
    context = multiprocessing.get_context("spawn")
    entered, output = context.Event(), context.Queue()
    # Spawn children reopen sync primitives BY NAME during unpickling, so
    # every primitive must outlive the spawn handshake from a named local;
    # inline temporaries inside args=(...) get unlinked first on Linux and
    # the child dies with SemLock._rebuild FileNotFoundError.
    barrier, release = context.Barrier(1), context.Event()
    process = context.Process(
        target=process_send,
        args=(
            database,
            tmp_path / "resumes",
            draft_id,
            barrier,
            entered,
            release,
            output,
            True,
        ),
    )
    try:
        process.start()
        process.join(15)
        assert process.exitcode == 0
        assert entered.is_set()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        output.close()
    before = read_delivery(database, draft_id)
    assert before["status"] == "sending"
    monkeypatch.setattr(
        "jobagent.applier.email_sent_verifier.EmailSentFolderVerifier.from_smtp_settings",
        lambda _: verifier,
    )
    settings = settings.model_copy(
        update={
            "jobagent_state_db": database,
            "jobagent_resume_dir": tmp_path / "resumes",
            "jobagent_workspace_root": tmp_path / "workspace",
            "jobagent_checkpoint_db": tmp_path / "checkpoints.db",
            "jobagent_artifact_dir": tmp_path / "artifacts",
            "jobagent_opportunity_dir": tmp_path / "opportunities",
            "jobagent_skills_dir": tmp_path / "skills",
        }
    )
    agent = build_job_agent(settings, model=FakeListChatModel(responses=["ok"]))
    assert agent is not None
    xhs_agent = next(item for item in agent._subagents if item["name"] == "xhs_recruiting")
    description = xhs_agent["interrupt_on"]["send_recruitment_email"]["description"]
    assert "draft_not_found" in description({"args": {"draft_id": "missing"}}, {}, None)
    assert verifier.calls == [before["message_id"]]
    assert read_delivery(database, draft_id)["sync_status"] == "done"
    assert read_delivery(database, draft_id)["status"] == "submitted"
    service.recover()
    assert len(verifier.calls) == 1
    assert not sender.calls
    with SQLiteJourneyStore(database) as store:
        assert store.get_journey(journey.id).stage == "applied"


@pytest.mark.asyncio
async def test_sent_missing_stays_unverified_until_later_hit(delivery):
    database, _, _, service, draft_id, journey, sender, verifier = delivery
    assert service._claim(draft_id) is None
    verifier.status = "unverified"
    service.recover()
    service.recover()
    assert (await service.send(draft_id))["status"] == "unverified"
    assert not sender.calls
    with SQLiteJourneyStore(database) as store:
        assert store.get_journey(journey.id).stage == "targeted"
    verifier.status = "submitted"
    service.recover()
    assert read_delivery(database, draft_id)["status"] == "submitted"
    assert (await service.send(draft_id))["error_type"] == "already_submitted"
    assert not sender.calls


@pytest.mark.parametrize("replay", ["send", "recover"])
@pytest.mark.asyncio
async def test_journey_failure_replays_only_state(delivery, monkeypatch, replay):
    database, _, _, service, draft_id, journey, sender, verifier = delivery
    original = SQLiteJourneyStore.mark_applied_by_creation_key

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(SQLiteJourneyStore, "mark_applied_by_creation_key", fail)
    response = await service.send(draft_id)
    assert response["status"] == "submitted"
    assert response["sync_status"] == "pending"
    assert read_delivery(database, draft_id)["status"] == "submitted"
    assert (await service.send(draft_id))["sync_status"] == "pending"
    monkeypatch.setattr(SQLiteJourneyStore, "mark_applied_by_creation_key", original)
    if replay == "send":
        assert (await service.send(draft_id))["sync_status"] == "done"
    else:
        service.recover()
    service.recover()
    assert read_delivery(database, draft_id)["sync_status"] == "done"
    assert len(sender.calls) == 1
    assert not verifier.calls
    with SQLiteJourneyStore(database) as store:
        assert store.get_journey(journey.id).stage == "applied"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM journey_change_events WHERE action = 'application_submitted'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_missing_draft_safe_in_preview_and_structured_tool(tmp_path):
    service = XhsEmailDraftService(
        tmp_path / "empty.db", ResumeLibrary(tmp_path / "resumes"), Settings(_env_file=None)
    )
    assert "draft_not_found" in service.approval_preview("missing")
    response = await build_send_recruitment_email_tool(service).ainvoke({"draft_id": "missing"})
    assert response == {"status": "failed", "error_type": "draft_not_found"}


@pytest.mark.asyncio
async def test_legacy_uncertain_without_message_id_never_resends(delivery):
    database, resumes, settings, _, draft_id, _, sender, _ = delivery
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE xhs_email_deliveries")
        connection.execute(
            "CREATE TABLE xhs_email_deliveries "
            "(draft_id TEXT PRIMARY KEY, status TEXT, receipt TEXT)"
        )
        connection.execute(
            "INSERT INTO xhs_email_deliveries VALUES (?, 'sending', '{}')", (draft_id,)
        )
    service = XhsEmailDraftService(database, resumes, settings, sender)
    service.recover()
    assert read_delivery(database, draft_id)["status"] == "unverified"
    assert (await service.send(draft_id))["status"] == "unverified"
    assert not sender.calls


@pytest.mark.asyncio
async def test_transport_exception_blocks_retry(delivery):
    _, _, _, service, draft_id, _, _, _ = delivery

    class BrokenSender:
        def send(self, **kwargs):
            raise RuntimeError("SMTP outcome unknown")

    service._sender = BrokenSender()
    assert (await service.send(draft_id))["status"] == "unverified"
    assert (await service.send(draft_id))["error_type"] == "delivery_unverified"


@pytest.mark.asyncio
async def test_stale_sent_miss_cannot_overwrite_smtp_success(delivery):
    database, _, _, service, draft_id, _, sender, _ = delivery
    entered, release = threading.Event(), threading.Event()

    class SlowVerifier:
        def find(self, message_id):
            entered.set()
            assert release.wait(10)
            return SentVerification("unverified", message_id, "not_found")

    service._verifier = SlowVerifier()
    assert service._claim(draft_id) is None
    message_id = read_delivery(database, draft_id)["message_id"]
    recovery = asyncio.create_task(asyncio.to_thread(service.recover))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        service._finish(draft_id, message_id, {"status": "ok"})
    finally:
        release.set()
    await recovery
    assert read_delivery(database, draft_id)["status"] == "submitted"
    assert not sender.calls


@pytest.mark.asyncio
async def test_concurrent_replays_create_only_one_journey_event(delivery):
    database, _, _, service, draft_id, _, sender, _ = delivery
    assert service._claim(draft_id) is None
    message_id = read_delivery(database, draft_id)["message_id"]
    service._finish(draft_id, message_id, {"status": "ok"})
    await asyncio.gather(*(asyncio.to_thread(service.recover) for _ in range(4)))
    assert read_delivery(database, draft_id)["sync_status"] == "done"
    assert not sender.calls
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM journey_change_events WHERE action = 'application_submitted'"
            ).fetchone()[0]
            == 1
        )
