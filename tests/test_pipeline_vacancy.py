"""CLI tests for pipeline_ctl vacancy details, waves and results.

Every test uses a fresh database under pytest's ``tmp_path`` and never touches
the repository's real ``db/``, ``companies/`` or ``master_data/``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(tmp_path, *args, now="2026-01-01T00:00:00Z"):
    """Run the pipeline_ctl CLI against a temp root and return the process."""
    return subprocess.run(
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


def add_vacancy(tmp_path):
    """Create the schema and one synthetic vacancy."""
    initialized = run(tmp_path, "--init")
    assert initialized.returncode == 0, initialized.stderr
    added = run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "T",
    )
    assert added.returncode == 0, added.stderr
    return added


def test_get_vacancy_returns_details(tmp_path):
    """--get-vacancy returns stored details with JSON columns parsed."""
    add_vacancy(tmp_path)

    from scripts.db import connect
    from scripts.pipeline_ctl import set_vacancy_details

    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        set_vacancy_details(
            conn,
            "v1",
            area="Москва",
            experience="От 3 до 6 лет",
            salary={
                "from": 100,
                "to": 200,
                "currency": "RUR",
                "gross": True,
            },
            hard_skills=["Python", "SQL"],
            employer_url="https://example.com/employer/1",
            description="текст",
            now="2026-01-01T00:00:00Z",
        )
    finally:
        conn.close()

    result = run(tmp_path, "--get-vacancy", "v1")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["area"] == "Москва"
    assert payload["experience"] == "От 3 до 6 лет"
    assert payload["salary"] == {
        "from": 100,
        "to": 200,
        "currency": "RUR",
        "gross": True,
    }
    assert payload["hard_skills"] == ["Python", "SQL"]
    assert payload["employer_url"] == "https://example.com/employer/1"
    assert payload["description"] == "текст"
    assert payload["company_name"] == "acme"
    assert payload["wave"] is None
    assert payload["result"] is None


def test_set_vacancy_details_stores_source_fields(tmp_path):
    """set_vacancy_details persists the six DB-backed source fields."""
    add_vacancy(tmp_path)

    from scripts.db import connect
    from scripts.pipeline_ctl import set_vacancy_details

    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        set_vacancy_details(
            conn,
            "v1",
            area="Москва",
            experience="От 3 до 6 лет",
            salary=None,
            hard_skills=["Python"],
            employer_url=None,
            description="текст",
            now="2026-01-01T00:00:00Z",
            source="fixture",
            source_id="123",
            api_url="https://example.com/vacancies/123",
            employer_id="55",
            injections=["ru_forget_previous"],
            fetched_ts="2026-01-01T00:00:00Z",
        )
    finally:
        conn.close()

    result = run(tmp_path, "--get-vacancy", "v1")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == "fixture"
    assert payload["source_id"] == "123"
    assert payload["api_url"] == "https://example.com/vacancies/123"
    assert payload["employer_id"] == "55"
    assert payload["injections"] == ["ru_forget_previous"]
    assert payload["fetched_ts"] == "2026-01-01T00:00:00Z"


def test_get_vacancy_returns_source_fields(tmp_path):
    """get_vacancy exposes the six persisted source columns."""
    add_vacancy(tmp_path)

    from scripts.db import connect
    from scripts.pipeline_ctl import get_vacancy, set_vacancy_details

    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        set_vacancy_details(
            conn,
            "v1",
            area="Санкт-Петербург",
            experience="Без опыта",
            salary=None,
            hard_skills=["SQL"],
            employer_url=None,
            description="синтетическое описание",
            now="2026-01-01T00:00:00Z",
            source="fixture",
            source_id="123",
            api_url="https://example.com/vacancies/123",
            employer_id="55",
            injections=["ru_forget_previous", "ru_system_override"],
            fetched_ts="2026-01-01T00:00:00Z",
        )
        payload = get_vacancy(conn, "v1")
    finally:
        conn.close()

    assert payload["source"] == "fixture"
    assert payload["source_id"] == "123"
    assert payload["api_url"] == "https://example.com/vacancies/123"
    assert payload["employer_id"] == "55"
    assert payload["injections"] == ["ru_forget_previous", "ru_system_override"]
    assert payload["fetched_ts"] == "2026-01-01T00:00:00Z"


def test_get_vacancy_not_found(tmp_path):
    """An unknown vacancy id fails on stderr with exit code 1."""
    initialized = run(tmp_path, "--init")
    assert initialized.returncode == 0, initialized.stderr

    result = run(tmp_path, "--get-vacancy", "nosuch")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == "vacancy_not_found"


def test_set_wave_and_summary(tmp_path):
    """--set-wave stores the batch and appends a wave column to --summary."""
    add_vacancy(tmp_path)

    wave = run(tmp_path, "--set-wave", "v1", "--wave", "2")
    assert wave.returncode == 0, wave.stderr
    assert json.loads(wave.stdout) == {
        "ok": True,
        "vacancy_id": "v1",
        "wave": 2,
    }

    summarized = run(tmp_path, "--summary")
    assert summarized.returncode == 0, summarized.stderr
    payload = json.loads(summarized.stdout)
    assert payload["cols"][-1] == "wave"
    assert len(payload["rows"]) == 1
    assert payload["rows"][0][-1] == 2


def test_set_result_and_get(tmp_path):
    """--set-result stores a free-form outcome visible via --get-vacancy."""
    add_vacancy(tmp_path)

    stored = run(
        tmp_path, "--set-result", "v1", "--result", "Интервью пройдено"
    )
    assert stored.returncode == 0, stored.stderr
    assert json.loads(stored.stdout) == {
        "ok": True,
        "vacancy_id": "v1",
        "result": "Интервью пройдено",
    }

    fetched = run(tmp_path, "--get-vacancy", "v1")
    assert fetched.returncode == 0, fetched.stderr
    assert json.loads(fetched.stdout)["result"] == "Интервью пройдено"

    missing = run(tmp_path, "--set-result", "nosuch", "--result", "x")
    assert missing.returncode == 1
    assert json.loads(missing.stderr)["error"] == "vacancy_not_found"


def test_set_wave_invalid(tmp_path):
    """A wave outside 1..3 is rejected before the row is looked up."""
    add_vacancy(tmp_path)

    invalid = run(tmp_path, "--set-wave", "v1", "--wave", "9")
    assert invalid.returncode == 1
    assert json.loads(invalid.stderr)["error"] == "invalid_wave"

    missing = run(tmp_path, "--set-wave", "nosuch", "--wave", "1")
    assert missing.returncode == 1
    assert json.loads(missing.stderr)["error"] == "vacancy_not_found"


def test_backfill_details_populates_vacancy(tmp_path):
    """--backfill-details fills the detail columns from the clean JSON."""
    add_vacancy(tmp_path)

    vacancy_dir = tmp_path / "companies" / "acme" / "vacancies" / "v1"
    vacancy_dir.mkdir(parents=True)
    clean = {
        "vacancy_id": "v1",
        "area": "Москва",
        "experience": "От 3 до 6 лет",
        "salary": {
            "from": 100,
            "to": 200,
            "currency": "RUR",
            "gross": True,
        },
        "hard_skills": ["Python", "SQL"],
        "employer_url": "https://example.com/employer/1",
        "description": "текст",
        "source": "fixture",
        "source_id": "123",
        "api_url": "https://example.com/vacancies/123",
        "employer_id": "55",
        "injections": ["ru_forget_previous"],
        "fetched_ts": "2026-01-01T00:00:00Z",
    }
    (vacancy_dir / "1_vacancy_clean.json").write_text(
        json.dumps(clean, ensure_ascii=False),
        encoding="utf-8",
    )

    backfilled = run(tmp_path, "--backfill-details")
    assert backfilled.returncode == 0, backfilled.stderr
    assert json.loads(backfilled.stdout) == {
        "ok": True,
        "filled": 1,
        "skipped_missing": [],
        "skipped_invalid": [],
    }

    fetched = run(tmp_path, "--get-vacancy", "v1")
    assert fetched.returncode == 0, fetched.stderr
    payload = json.loads(fetched.stdout)
    assert payload["area"] == "Москва"
    assert payload["experience"] == "От 3 до 6 лет"
    assert payload["salary"] == {
        "from": 100,
        "to": 200,
        "currency": "RUR",
        "gross": True,
    }
    assert payload["hard_skills"] == ["Python", "SQL"]
    assert payload["employer_url"] == "https://example.com/employer/1"
    assert payload["description"] == "текст"
    assert payload["source"] == "fixture"
    assert payload["source_id"] == "123"
    assert payload["api_url"] == "https://example.com/vacancies/123"
    assert payload["employer_id"] == "55"
    assert payload["injections"] == ["ru_forget_previous"]
    assert payload["fetched_ts"] == "2026-01-01T00:00:00Z"


def test_backfill_details_skips_missing_vacancy(tmp_path):
    """A clean JSON whose vacancy_id is not in the DB lands in skipped_missing."""
    initialized = run(tmp_path, "--init")
    assert initialized.returncode == 0, initialized.stderr

    ghost_dir = tmp_path / "companies" / "ghost" / "vacancies" / "ghost"
    ghost_dir.mkdir(parents=True)
    (ghost_dir / "1_vacancy_clean.json").write_text(
        json.dumps(
            {"vacancy_id": "ghost", "area": "Москва"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    backfilled = run(tmp_path, "--backfill-details")
    assert backfilled.returncode == 0, backfilled.stderr
    payload = json.loads(backfilled.stdout)
    assert payload["filled"] == 0
    assert payload["skipped_missing"] == ["ghost"]


def test_backfill_details_skips_invalid_json(tmp_path):
    """A clean file that is not JSON lands in skipped_invalid."""
    add_vacancy(tmp_path)

    vacancy_dir = tmp_path / "companies" / "acme" / "vacancies" / "v1"
    vacancy_dir.mkdir(parents=True)
    (vacancy_dir / "1_vacancy_clean.json").write_text(
        "not valid json",
        encoding="utf-8",
    )

    backfilled = run(tmp_path, "--backfill-details")
    assert backfilled.returncode == 0, backfilled.stderr
    payload = json.loads(backfilled.stdout)
    assert payload["filled"] == 0
    assert (
        "companies/acme/vacancies/v1/1_vacancy_clean.json"
        in payload["skipped_invalid"]
    )
