"""CLI tests for scripts.pipeline_ctl cache, company and vacancy state.

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


def test_init_creates_db_and_categories(tmp_path):
    """--init creates the schema and seeds the five categories, no vault file."""
    result = run(tmp_path, "--init")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["db"] == "db/state.sqlite"
    assert payload["schema_version"] == 4
    assert payload["categories"] == 5

    vault_file = tmp_path / "master_data" / "skills_vault.json"
    assert not vault_file.exists()


def test_check_url_allow_when_unknown(tmp_path):
    """An unseen URL is allowed to be scraped."""
    result = run(tmp_path, "--check-url", "https://example.com/vacancy/1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": "ALLOW_SCRAPE",
        "last_scraped_ts": None,
        "age_days": None,
    }


def test_check_url_skip_within_90_days(tmp_path):
    """A URL marked 89 days ago is still served from cache."""
    url = "https://example.com/vacancy/2"
    marked = run(tmp_path, "--mark-url", url, now="2026-01-01T00:00:00Z")
    assert marked.returncode == 0, marked.stderr

    result = run(
        tmp_path, "--check-url", url, now="2026-03-31T00:00:00Z"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"] == "CACHE_SKIP"
    assert payload["age_days"] == 89.0


def test_check_url_allow_after_90_days(tmp_path):
    """Exactly 90 days old is expired; later timestamps are also expired."""
    url = "https://example.com/vacancy/3"
    marked = run(tmp_path, "--mark-url", url, now="2026-01-01T00:00:00Z")
    assert marked.returncode == 0, marked.stderr

    at_90 = run(tmp_path, "--check-url", url, now="2026-04-01T00:00:00Z")
    assert at_90.returncode == 0, at_90.stderr
    payload = json.loads(at_90.stdout)
    assert payload["result"] == "ALLOW_SCRAPE"
    assert payload["age_days"] == 90.0

    later = run(tmp_path, "--check-url", url, now="2026-06-01T00:00:00Z")
    assert later.returncode == 0, later.stderr
    assert json.loads(later.stdout)["result"] == "ALLOW_SCRAPE"


def test_check_company_cache_hit_and_expired(tmp_path):
    """A fresh dossier is a cache hit; a 120-day-old one has expired."""
    added = run(
        tmp_path,
        "--add-vacancy",
        "v1",
        "--company",
        "acme",
        "--title",
        "Разработчик",
    )
    assert added.returncode == 0, added.stderr

    dossier = tmp_path / "companies" / "acme" / "exec_dossier.md"
    dossier.parent.mkdir(parents=True, exist_ok=True)
    dossier.write_text(
        "# АО «Акме»\n\nКомпания разрабатывает ПО.\n", encoding="utf-8"
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

    fresh = run(tmp_path, "--check-company", "acme", now="2026-01-31T00:00:00Z")
    assert fresh.returncode == 0, fresh.stderr
    assert json.loads(fresh.stdout) == {
        "result": "CACHE_HIT",
        "dossier_path": "companies/acme/exec_dossier.md",
        "age_days": 30.0,
    }

    expired = run(
        tmp_path, "--check-company", "acme", now="2026-05-01T00:00:00Z"
    )
    assert expired.returncode == 0, expired.stderr
    assert json.loads(expired.stdout) == {
        "result": "NEEDS_OSINT",
        "reason": "expired",
    }


def test_check_company_no_dossier(tmp_path):
    """A company without a dossier needs OSINT; an unknown slug has no row."""
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

    no_dossier = run(tmp_path, "--check-company", "acme")
    assert no_dossier.returncode == 0, no_dossier.stderr
    payload = json.loads(no_dossier.stdout)
    assert payload["result"] == "NEEDS_OSINT"
    assert payload["reason"] == "no_dossier"

    no_company = run(tmp_path, "--check-company", "unknown-co")
    assert no_company.returncode == 0, no_company.stderr
    payload = json.loads(no_company.stdout)
    assert payload["result"] == "NEEDS_OSINT"
    assert payload["reason"] == "no_company"


def test_update_vacancy_increments_qa_attempts(tmp_path):
    """Only QA_FAILED_* transitions bump qa_attempts; a score is persisted."""
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

    failed = run(
        tmp_path, "--update-vacancy", "v1", "--status", "QA_FAILED_WRITER"
    )
    assert failed.returncode == 0, failed.stderr
    assert json.loads(failed.stdout)["qa_attempts"] == 1

    ready = run(
        tmp_path,
        "--update-vacancy",
        "v1",
        "--status",
        "PLAN_READY",
        "--score",
        "80",
    )
    assert ready.returncode == 0, ready.stderr
    payload = json.loads(ready.stdout)
    assert payload["qa_attempts"] == 1
    assert payload["match_score"] == 80

    failed_again = run(
        tmp_path, "--update-vacancy", "v1", "--status", "QA_FAILED_STRATEGY"
    )
    assert failed_again.returncode == 0, failed_again.stderr
    payload = json.loads(failed_again.stdout)
    assert payload["qa_attempts"] == 2
    assert payload["match_score"] == 80


def test_invalid_status_exit_1(tmp_path):
    """An unknown status fails on stderr with exit code 1 and empty stdout."""
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

    result = run(tmp_path, "--update-vacancy", "v1", "--status", "BOGUS")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == "invalid_status"
