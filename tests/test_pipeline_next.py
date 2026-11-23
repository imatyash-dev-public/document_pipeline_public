"""Tests for scripts.pipeline_ctl --next loop breaker and --summary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(
    tmp_path: Path,
    *args: str,
    now: str = "2026-01-01T00:00:00Z",
) -> dict:
    """Run the pipeline_ctl CLI against a fresh temp root and parse its JSON."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.pipeline_ctl",
            "--root",
            str(tmp_path),
            "--now",
            now,
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_next_idle_when_empty(tmp_path: Path) -> None:
    """--next on an empty database reports IDLE with nothing remaining."""
    assert run(tmp_path, "--next") == {
        "action": "IDLE",
        "remaining": 0,
        "halted": [],
    }


def test_next_priority_order(tmp_path: Path) -> None:
    """QA failures outrank drafts, which outrank fresh discoveries."""
    run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:01Z",
    )
    run(
        tmp_path,
        "--add-vacancy",
        "v2",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:02Z",
    )
    run(
        tmp_path,
        "--add-vacancy",
        "v3",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:03Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v2",
        "--status",
        "DRAFT_READY",
        now="2026-01-01T01:00:00Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v3",
        "--status",
        "QA_FAILED_WRITER",
        now="2026-01-01T02:00:00Z",
    )

    result = run(tmp_path, "--next", now="2026-01-02T00:00:00Z")

    assert result["action"] == "REWRITE_DRAFT"
    assert result["vacancy_id"] == "v3"
    assert result["qa_attempts"] == 1
    assert result["remaining"] == 3
    assert result["halted"] == []


def test_loop_breaker_halts_after_two_failures(tmp_path: Path) -> None:
    """Two writer failures halt a vacancy and the next pick moves on."""
    run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:01Z",
    )
    run(
        tmp_path,
        "--add-vacancy",
        "v2",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:02Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v1",
        "--status",
        "QA_FAILED_WRITER",
        now="2026-01-01T01:00:00Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v1",
        "--status",
        "QA_FAILED_WRITER",
        now="2026-01-01T02:00:00Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v2",
        "--status",
        "PLAN_READY",
        now="2026-01-01T03:00:00Z",
    )

    result = run(tmp_path, "--next", now="2026-01-02T00:00:00Z")

    assert result["halted"] == ["v1"]
    assert result["action"] == "WRITE_DRAFT"
    assert result["vacancy_id"] == "v2"
    assert result["remaining"] == 1

    summary = run(tmp_path, "--summary", now="2026-01-02T00:00:00Z")
    assert summary["cols"] == [
        "vacancy_id",
        "company_slug",
        "status",
        "match_score",
        "qa_attempts",
        "wave",
    ]
    row = next(r for r in summary["rows"] if r[0] == "v1")
    assert row[2] == "HALTED_MAX_RETRIES"
    assert row[4] == 2

    again = run(tmp_path, "--next", now="2026-01-02T00:00:00Z")
    assert again["halted"] == []


def test_single_failure_not_halted(tmp_path: Path) -> None:
    """A single QA failure still routes back to a plan rebuild."""
    run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:01Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v1",
        "--status",
        "QA_FAILED_STRATEGY",
        now="2026-01-01T01:00:00Z",
    )

    result = run(tmp_path, "--next", now="2026-01-02T00:00:00Z")

    assert result["action"] == "REBUILD_PLAN"
    assert result["qa_attempts"] == 1
    assert result["halted"] == []


def test_next_osint_routing(tmp_path: Path) -> None:
    """A scored discovery runs OSINT, then reuses a fresh dossier."""
    run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:01Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v1",
        "--status",
        "DISCOVERED",
        "--score",
        "85",
        now="2026-01-01T01:00:00Z",
    )

    first = run(tmp_path, "--next", now="2026-01-02T00:00:00Z")
    assert first["action"] == "RUN_OSINT"

    dossier = tmp_path / "companies" / "acme" / "exec_dossier.md"
    dossier.parent.mkdir(parents=True, exist_ok=True)
    dossier.write_text(
        "# Досье компании Acme\n\nКомпания занимается разработкой ПО.\n",
        encoding="utf-8",
    )

    from scripts.db import connect
    from scripts.pipeline_ctl import set_company_osint

    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        set_company_osint(
            conn,
            "acme",
            "companies/acme/exec_dossier.md",
            "2026-01-01T00:00:00Z",
        )
    finally:
        conn.close()

    second = run(tmp_path, "--next", now="2026-01-02T00:00:00Z")
    assert second["action"] == "REUSE_OSINT"
    assert second["dossier_path"] == "companies/acme/exec_dossier.md"


def test_summary_counts(tmp_path: Path) -> None:
    """--summary reports totals, status counts and ordered rows."""
    run(
        tmp_path,
        "--add-vacancy",
        "a1",
        "--company",
        "beta",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:01Z",
    )
    run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:02Z",
    )
    run(
        tmp_path,
        "--add-vacancy",
        "v2",
        "--company",
        "acme",
        "--title",
        "Разработчик",
        now="2026-01-01T00:00:03Z",
    )
    run(
        tmp_path,
        "--update-vacancy",
        "v2",
        "--status",
        "QA_PASSED",
        now="2026-01-01T01:00:00Z",
    )

    summary = run(tmp_path, "--summary", now="2026-01-02T00:00:00Z")

    assert summary["total"] == 3
    assert summary["by_status"] == {"DISCOVERED": 2, "QA_PASSED": 1}
    assert summary["cols"] == [
        "vacancy_id",
        "company_slug",
        "status",
        "match_score",
        "qa_attempts",
        "wave",
    ]
    assert [r[0] for r in summary["rows"]] == ["v1", "v2", "a1"]
