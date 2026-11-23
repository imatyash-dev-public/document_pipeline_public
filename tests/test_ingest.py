"""Contract tests for the offline scripts.ingest adapter and fixture tooling."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.ingest as ing
import scripts.pipeline_ctl as pctl
from scripts import common
from scripts.db import connect


NOW = "2026-01-01T00:00:00Z"
SOURCE_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic_sources"


def test_adapter_search_reads_fixture() -> None:
    """search() returns the exact search fixture payload."""
    adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
    data = adapter.search("engineer", "113", 20, 0)
    assert data == {
        "found": 3,
        "items": [{"id": "1001"}, {"id": "1002"}, {"id": "1003"}],
    }


def test_adapter_fetch_details_json() -> None:
    """fetch_details() reads a JSON vacancy fixture."""
    adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
    detail = adapter.fetch_details("1001")
    assert detail["name"] == "Senior Data Engineer"
    assert detail["match_score"] == 92
    assert isinstance(detail["key_skills"], list)
    assert {"name": "Python"} in detail["key_skills"]


def test_adapter_fetch_details_html_jsonld() -> None:
    """fetch_details() parses a schema.org JobPosting JSON-LD HTML fixture."""
    adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
    detail = adapter.fetch_details("1003")
    assert detail["name"] == "Data Platform Architect"
    assert detail["employer"]["name"] == "CloudGrid"
    assert detail["match_score"] == 85
    names = {skill["name"] for skill in detail["key_skills"]}
    assert {"Python", "SQL", "Docker"} <= names


def test_adversarial_fixture_injection_redacted() -> None:
    """build_vacancy_record() redacts the adversarial prompt injection."""
    adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
    rec = ing.build_vacancy_record(adapter.fetch_details("1003"), NOW)
    assert "[REDACTED_INJECTION_ATTEMPT]" in rec["description"]
    assert "ignore previous instructions" not in rec["description"].lower()
    assert rec["injections"]


def test_search_and_persist_creates_vacancy(tmp_path) -> None:
    """search_and_persist() saves every fixture vacancy as DISCOVERED."""
    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
        res = ing.search_and_persist(
            conn, tmp_path, adapter, "engineer", "113", 20, 0, NOW
        )
    finally:
        conn.close()

    assert res["ok"] is True
    assert res["found"] == 3
    assert len(res["saved"]) == 3

    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        assert pctl.get_vacancy(conn, "1001")["status"] == "DISCOVERED"
    finally:
        conn.close()


def test_search_skips_cached(tmp_path) -> None:
    """A second identical search skips all cached fixture vacancies."""
    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
        ing.search_and_persist(
            conn, tmp_path, adapter, "engineer", "113", 20, 0, NOW
        )
        res2 = ing.search_and_persist(
            conn, tmp_path, adapter, "engineer", "113", 20, 0, NOW
        )
    finally:
        conn.close()

    assert res2["saved"] == []
    assert res2["skipped_cached"] == ["1001", "1002", "1003"]


def test_market_scan_stats() -> None:
    """market_scan() aggregates fixture salary counts and RUR statistics."""
    adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
    stats = ing.market_scan(adapter, "engineer", "113", 20, 0)

    assert stats["ok"] is True
    assert stats["scanned"] == 5
    assert stats["with_salary"] == 4
    assert stats["without_salary"] == 1
    assert stats["currencies"] == {"RUR": 3, "USD": 1}
    assert isinstance(stats["salary"], dict)
    assert stats["salary"]["currency"] == "RUR"
    assert stats["salary"]["sample"] == 3


def test_market_scan_no_db_writes(tmp_path) -> None:
    """market_scan() is read-only and creates no db/ or companies/ dirs."""
    adapter = ing.FixtureIngestionAdapter(SOURCE_DIR)
    ing.market_scan(adapter, "engineer", "113", 20, 0)

    assert not (tmp_path / "db").exists()
    assert not (tmp_path / "companies").exists()


def test_save_osint_updates_company(tmp_path) -> None:
    """save_osint() writes the dossier and registers a fresh OSINT cache."""
    notes_path = tmp_path / "notes.md"
    notes_path.write_text(
        "# Acme\n\nКомпания Acme — облачные данные и аналитика.\n",
        encoding="utf-8",
    )
    conn = connect(tmp_path / "db" / "state.sqlite")
    try:
        res = ing.save_osint(conn, tmp_path, "acme", notes_path, NOW)
        check = pctl.check_company(conn, tmp_path, "acme", NOW)
    finally:
        conn.close()

    assert res["ok"] is True
    assert res["dossier_path"] == "companies/acme/exec_dossier.md"
    assert check["result"] == "CACHE_HIT"


def test_missing_source_dir_error() -> None:
    """A missing fixture directory raises a source_dir_not_found CliError."""
    with pytest.raises(common.CliError) as excinfo:
        ing.FixtureIngestionAdapter(Path("/nonexistent-dir-xyz"))
    assert excinfo.value.code == "source_dir_not_found"