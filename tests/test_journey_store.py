"""Behavior tests for durable Opportunity Journey state."""

from pathlib import Path

import pytest

from jobagent.journey import ArtifactStatus, SQLiteJourneyStore, TaskStatus


def test_validated_artifact_advances_task_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "jobagent.db"
    store = SQLiteJourneyStore(database)
    journey = store.create_journey(
        company="字节跳动",
        role="后端开发",
        job_description="负责分布式服务",
    )
    task = store.start_task(journey.id, "interview_research")
    artifact = store.create_artifact(
        journey_id=journey.id,
        task_run_id=task.id,
        artifact_type="raw_source_snapshot",
        content_ref="data/xhs/note-1/manifest.json",
        content_hash="sha256:test",
        provenance_refs=["xhs:note-1"],
    )

    with pytest.raises(ValueError, match="accepted artifacts"):
        store.complete_task(task.id, [artifact.id])

    accepted = store.validate_artifact(artifact.id, valid=True)
    completed = store.complete_task(task.id, [artifact.id])
    store.close()

    reopened = SQLiteJourneyStore(database)
    assert reopened.get_journey(journey.id) == journey
    assert reopened.get_artifact(artifact.id).status is ArtifactStatus.ACCEPTED
    assert accepted.status is ArtifactStatus.ACCEPTED
    assert reopened.get_task(task.id).status is TaskStatus.SUCCEEDED
    assert completed.output_artifact_ids == (artifact.id,)
    reopened.close()
