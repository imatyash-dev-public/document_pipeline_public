"""CLI tests for ``pipeline_ctl --gate-check`` (DeepSeek peak-hour gate).

Every test runs the CLI via subprocess against a fresh root under pytest's
``tmp_path``; nothing here touches the repository's real ``db/``,
``companies/`` or ``master_data/``, and no test uses the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

GATE_OK = {"result": "GATE_OK", "window": None, "opens_at": None}


def run(tmp_path, *args):
    """Run the gate-check CLI against a temp root and return the process."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.pipeline_ctl",
            "--root",
            str(tmp_path),
            "--gate-check",
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
    )


def test_gate_ok_before_window1(tmp_path):
    """One second before weekday window 1 opens, the gate is open."""
    result = run(tmp_path, "--now", "2026-11-16T00:44:59Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == GATE_OK


def test_gate_blocked_window1_start(tmp_path):
    """Exactly at 00:45Z on a Monday, window 1 blocks until 04:00Z."""
    result = run(tmp_path, "--now", "2026-11-16T00:45:00Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": "GATE_BLOCKED",
        "window": "00:45-04:00Z",
        "opens_at": "2026-11-16T04:00:00Z",
    }


def test_gate_blocked_window1_last_second(tmp_path):
    """The last second of window 1 is still blocked until 04:00Z."""
    result = run(tmp_path, "--now", "2026-11-16T03:59:59Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": "GATE_BLOCKED",
        "window": "00:45-04:00Z",
        "opens_at": "2026-11-16T04:00:00Z",
    }


def test_gate_ok_at_window1_end(tmp_path):
    """The 04:00Z boundary itself is outside window 1."""
    result = run(tmp_path, "--now", "2026-11-16T04:00:00Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == GATE_OK


def test_gate_blocked_window2_start(tmp_path):
    """Exactly at 05:45Z on a Monday, window 2 blocks until 10:00Z."""
    result = run(tmp_path, "--now", "2026-11-16T05:45:00Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": "GATE_BLOCKED",
        "window": "05:45-10:00Z",
        "opens_at": "2026-11-16T10:00:00Z",
    }


def test_gate_ok_at_window2_end(tmp_path):
    """The 10:00Z boundary itself is outside window 2."""
    result = run(tmp_path, "--now", "2026-11-16T10:00:00Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == GATE_OK


def test_gate_weekend_always_ok(tmp_path):
    """Saturday 01:30Z is inside weekday window 1 but the gate stays open."""
    result = run(tmp_path, "--now", "2026-11-14T01:30:00Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == GATE_OK


def test_gate_malformed_now_exit_1(tmp_path):
    """A malformed --now fails on stderr with invalid_timestamp, exit 1."""
    result = run(tmp_path, "--now", "not-a-timestamp")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == "invalid_timestamp"


def test_gate_check_creates_no_db(tmp_path):
    """Gate-check never creates or opens the SQLite database."""
    result = run(tmp_path, "--now", "2026-11-16T01:00:00Z")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == "GATE_BLOCKED"
    assert (tmp_path / "db").exists() is False
    assert (tmp_path / "db" / "state.sqlite").exists() is False
