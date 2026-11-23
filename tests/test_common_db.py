"""Tests for scripts.common helpers and the scripts.db schema layer."""

from __future__ import annotations

import sqlite3

from scripts.common import is_fresh, slugify
from scripts.db import (
    SCHEMA_VERSION,
    SEED_CATEGORIES,
    connect,
    migrate_1,
    migrate_2,
    migrate_3,
    migrate_4,
)


def test_slugify_cyrillic() -> None:
    """Cyrillic company names transliterate to an ASCII slug."""
    assert slugify("ООО Ромашка & Co") == "ooo-romashka-co"
    assert slugify("!!!") == "company"


def test_is_fresh_boundary() -> None:
    """A cache entry is fresh below 90 days and expired at exactly 90 days."""
    ts = "2026-01-01T00:00:00Z"
    assert is_fresh(ts, "2026-03-31T23:45:00Z") is True
    assert is_fresh(ts, "2026-04-01T00:00:00Z") is False
    assert is_fresh(None, "2026-01-01T00:00:00Z") is False


def test_connect_schema_version(tmp_path) -> None:
    """connect() applies all migrations and enables foreign keys."""
    db_path = tmp_path / "t.sqlite"
    conn = connect(db_path)
    try:
        assert SCHEMA_VERSION == 4
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"companies", "vacancies", "url_cache"} <= tables
    finally:
        conn.close()

    conn2 = connect(db_path)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(vacancies)")}
        assert "wave" in cols
    finally:
        conn2.close()


def test_migrate_2_adds_wave_and_detail_columns(tmp_path) -> None:
    """Migration 2 adds the wave and vacancy-detail columns to vacancies."""
    conn = sqlite3.connect(str(tmp_path / "m2.sqlite"))
    try:
        migrate_1(conn)
        migrate_2(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(vacancies)")}
        assert {
            "wave",
            "result",
            "area",
            "experience",
            "salary",
            "hard_skills",
            "employer_url",
            "description",
        } <= cols
    finally:
        conn.close()


def test_migrate_3_adds_skill_tables_and_vacancy_columns(tmp_path) -> None:
    """Migration 3 adds the skill tables, seeds categories and vacancy sources."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "skill_categories",
            "skills",
            "skill_aliases",
            "pending_skills",
            "pending_skill_vacancies",
        } <= tables

        categories = conn.execute(
            "SELECT name FROM skill_categories ORDER BY sort_order"
        ).fetchall()
        assert len(categories) == 5
        assert tuple(row[0] for row in categories) == SEED_CATEGORIES

        cols = {row[1] for row in conn.execute("PRAGMA table_info(vacancies)")}
        assert {
            "source",
            "source_id",
            "api_url",
            "employer_id",
            "injections",
            "fetched_ts",
        } <= cols
    finally:
        conn.close()


def test_migrate_4_adds_dispatch_statuses(tmp_path) -> None:
    """Fresh and legacy v3 databases accept the new DISPATCHED/CLOSED statuses."""
    now = "2026-01-01T00:00:00Z"
    title = "Инженер данных"
    insert_vacancy = (
        "INSERT INTO vacancies("
        "vacancy_id, company_slug, title, status, created_ts, updated_ts"
        ") VALUES (?, ?, ?, ?, ?, ?)"
    )

    fresh = connect(tmp_path / "fresh.sqlite")
    try:
        fresh.execute(
            "INSERT INTO companies(company_slug, company_name) VALUES (?, ?)",
            ("acme", "Acme"),
        )
        for vacancy_id, status in (("fresh-1", "DISPATCHED"), ("fresh-2", "CLOSED")):
            fresh.execute(
                insert_vacancy,
                (vacancy_id, "acme", title, status, now, now),
            )
        fresh.commit()
        assert fresh.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        fresh.close()

    legacy = sqlite3.connect(str(tmp_path / "legacy.sqlite"))
    try:
        migrate_1(legacy)
        migrate_2(legacy)
        migrate_3(legacy)
        legacy.execute(
            "INSERT INTO companies(company_slug, company_name) VALUES (?, ?)",
            ("legacy-co", "Legacy Co"),
        )
        legacy.execute(
            insert_vacancy,
            ("legacy-1", "legacy-co", title, "DISCOVERED", now, now),
        )
        legacy.commit()
        migrate_4(legacy)
        legacy.execute(
            insert_vacancy,
            ("legacy-2", "legacy-co", title, "CLOSED", now, now),
        )
        legacy.commit()
        kept = legacy.execute(
            "SELECT COUNT(*) FROM vacancies WHERE status = ?", ("DISCOVERED",)
        ).fetchone()[0]
        assert kept == 1
    finally:
        legacy.close()

