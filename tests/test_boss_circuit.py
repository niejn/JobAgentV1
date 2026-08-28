"""Tests for the Boss circuit breaker (TR-6b)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path

PAGE_KILLED = {"status": "failed", "error_type": "page_lost"}


def _circuit(tmp_path: Path) -> BossCircuit:
    return BossCircuit(boss_circuit_path(tmp_path / "state.db"))


def test_three_consecutive_kills_trip_the_breaker(tmp_path: Path) -> None:
    circuit = _circuit(tmp_path)
    for _ in range(3):
        circuit.record(PAGE_KILLED)

    refusal = circuit.check()
    assert refusal is not None
    assert refusal["error_type"] == "circuit_open"
    assert "熔断" in refusal["message"]


def test_two_kills_do_not_trip_and_success_resets(tmp_path: Path) -> None:
    circuit = _circuit(tmp_path)
    circuit.record(PAGE_KILLED)
    circuit.record(PAGE_KILLED)
    assert circuit.check() is None  # below threshold: still allowed

    circuit.record({"status": "ok"})
    assert circuit.check() is None
    # counter reset: needs 3 MORE kills to trip
    circuit.record(PAGE_KILLED)
    circuit.record(PAGE_KILLED)
    assert circuit.check() is None


def test_non_trip_errors_are_ignored(tmp_path: Path) -> None:
    circuit = _circuit(tmp_path)
    for _ in range(5):
        circuit.record({"status": "failed", "error_type": "attachment_limit"})
    assert circuit.check() is None


def test_cooldown_expiry_resets(tmp_path: Path) -> None:
    circuit = _circuit(tmp_path)
    for _ in range(3):
        circuit.record(PAGE_KILLED)
    assert circuit.check() is not None

    # force the stored deadline into the past
    state = json.loads(circuit._path.read_text(encoding="utf-8"))
    past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
    state["until"] = past
    circuit._path.write_text(json.dumps(state), encoding="utf-8")

    assert circuit.check() is None  # expired -> reset, calls allowed


def test_state_survives_across_processes(tmp_path: Path) -> None:
    first = _circuit(tmp_path)
    for _ in range(3):
        first.record(PAGE_KILLED)
    # a NEW instance (next `chat -c` process) must see the same state
    assert _circuit(tmp_path).check() is not None


def test_env_bypass_forces_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    circuit = _circuit(tmp_path)
    for _ in range(3):
        circuit.record(PAGE_KILLED)
    assert circuit.check() is not None

    monkeypatch.setenv("JOBAGENT_BOSS_IGNORE_CIRCUIT", "1")
    assert circuit.check() is None  # forced through for debugging

    monkeypatch.setenv("JOBAGENT_BOSS_IGNORE_CIRCUIT", "false")
    assert circuit.check() is not None  # non-truthy values keep the gate
