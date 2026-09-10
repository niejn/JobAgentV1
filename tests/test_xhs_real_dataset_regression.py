import json
from pathlib import Path

import pytest

from jobagent.journey.recruitment_notes import extract_note

DATASET = (
    Path("data/xhs/冇名有姓_5b31e6b2e8ac2b1d8c943c44")
    / "Ai初创团队招人啦！研发&增长运营～_6a82b8df0000000025014840"
)


@pytest.mark.skipif(not DATASET.is_dir(), reason="local XHS regression dataset is unavailable")
def test_real_xhs_recruitment_note_extracts_both_roles():
    info = json.loads((DATASET / "info.json").read_text(encoding="utf-8"))
    note = extract_note(
        "6a82b8df0000000025014840",
        str(info["note_url"]),
        str(info["title"]),
        (DATASET / "detail.txt").read_text(encoding="utf-8"),
        "",
    )
    titles = {position.title for position in note.positions}
    assert "Agent 工程师" in titles
    assert "增长运营" in titles
    assert "AI Agent" in note.features
