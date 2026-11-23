"""SQLite connection, schema and migrations for the document pipeline.

Stdlib ``sqlite3`` only. Every connection runs with ``PRAGMA foreign_keys = ON``
and rows are exposed through ``sqlite3.Row``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import common


VACANCY_STATUSES = (
    "DISCOVERED",
    "SKIPPED_LOW_MATCH",
    "OSINT_READY",
    "AWAITING_SKILL_CLARIFICATION",
    "PLAN_READY",
    "DRAFT_READY",
    "QA_FAILED_WRITER",
    "QA_FAILED_STRATEGY",
    "QA_PASSED",
    "DISPATCHED",
    "HALTED_MAX_RETRIES",
    "CLOSED",
)

TERMINAL_STATUSES = (
    "SKIPPED_LOW_MATCH",
    "QA_PASSED",
    "HALTED_MAX_RETRIES",
    "DISPATCHED",
    "CLOSED",
)

SEED_CATEGORIES: tuple[str, ...] = (
    "Системный и бизнес-анализ",
    "Инженерия данных и аналитика",
    "Data Quality, статистика и 152-ФЗ",
    "ИИ и мультиагентные системы",
    "Доменная экспертиза и инфраструктура",
)

SCHEMA_VERSION = 4


def _status_literals() -> str:
    """Return the vacancy statuses as a comma-separated quoted SQL list."""
    return ", ".join("'" + status + "'" for status in VACANCY_STATUSES)


def _vacancies_ddl() -> str:
    """Return the full ``vacancies`` CREATE TABLE DDL for the final schema."""
    return (
        "CREATE TABLE vacancies ("
        "vacancy_id TEXT PRIMARY KEY, "
        "company_slug TEXT NOT NULL REFERENCES companies(company_slug), "
        "title TEXT NOT NULL, "
        "url TEXT, "
        "match_score INTEGER, "
        "status TEXT NOT NULL DEFAULT \'DISCOVERED\' "
        f"CHECK (status IN ({_status_literals()})), "
        "qa_attempts INTEGER NOT NULL DEFAULT 0, "
        "folder_path TEXT, "
        "created_ts TEXT NOT NULL, "
        "updated_ts TEXT NOT NULL, "
        "wave INTEGER, "
        "result TEXT, "
        "area TEXT, "
        "experience TEXT, "
        "salary TEXT, "
        "hard_skills TEXT, "
        "employer_url TEXT, "
        "description TEXT, "
        "source TEXT, "
        "source_id TEXT, "
        "api_url TEXT, "
        "employer_id TEXT, "
        "injections TEXT, "
        "fetched_ts TEXT"
        ")"
    )


def _add_missing_column(
    conn: sqlite3.Connection, column: str, declaration: str
) -> None:
    """Add a vacancies column only when it is not already present."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(vacancies)")}
    if column not in existing:
        conn.execute(f"ALTER TABLE vacancies ADD COLUMN {column} {declaration}")


def migrate_1(conn: sqlite3.Connection) -> None:
    """Create the initial schema: companies, vacancies and url_cache."""
    conn.execute(
        "CREATE TABLE companies ("
        "company_slug TEXT PRIMARY KEY, "
        "company_name TEXT NOT NULL, "
        "domain TEXT, "
        "dossier_path TEXT, "
        "last_osint_ts TEXT"
        ")"
    )
    conn.execute(_vacancies_ddl())
    conn.execute(
        "CREATE TABLE url_cache ("
        "url_hash TEXT PRIMARY KEY, "
        "url TEXT NOT NULL, "
        "last_scraped_ts TEXT NOT NULL, "
        "status_code INTEGER"
        ")"
    )
    conn.execute("CREATE INDEX idx_vacancies_status ON vacancies(status)")


def migrate_2(conn: sqlite3.Connection) -> None:
    """Add wave, result and vacancy detail columns to vacancies."""
    _add_missing_column(conn, "wave", "INTEGER")
    _add_missing_column(conn, "result", "TEXT")
    _add_missing_column(conn, "area", "TEXT")
    _add_missing_column(conn, "experience", "TEXT")
    _add_missing_column(conn, "salary", "TEXT")
    _add_missing_column(conn, "hard_skills", "TEXT")
    _add_missing_column(conn, "employer_url", "TEXT")
    _add_missing_column(conn, "description", "TEXT")


def migrate_3(conn: sqlite3.Connection) -> None:
    """Add skill tables, seed categories and extend vacancy source columns."""
    conn.execute(
        "CREATE TABLE skill_categories ("
        "category_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "sort_order INTEGER NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE skills ("
        "normalized TEXT PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "category_id INTEGER REFERENCES skill_categories(category_id) "
        "ON DELETE SET NULL, "
        "bucket TEXT NOT NULL CHECK (bucket IN "
        "('confirmed','rusty','learnable','missing','generic')), "
        "note TEXT NOT NULL DEFAULT '', "
        "added_ts TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE skill_aliases ("
        "alias TEXT PRIMARY KEY, "
        "canonical TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE pending_skills ("
        "normalized TEXT PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "first_seen TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE pending_skill_vacancies ("
        "normalized TEXT NOT NULL REFERENCES pending_skills(normalized) "
        "ON DELETE CASCADE, "
        "vacancy_id TEXT NOT NULL REFERENCES vacancies(vacancy_id) "
        "ON DELETE CASCADE, "
        "PRIMARY KEY (normalized, vacancy_id)"
        ")"
    )
    conn.execute("CREATE INDEX idx_skills_category ON skills(category_id)")
    conn.execute("CREATE INDEX idx_skills_bucket ON skills(bucket)")
    for sort_order, name in enumerate(SEED_CATEGORIES, start=1):
        conn.execute(
            "INSERT OR IGNORE INTO skill_categories(name, sort_order) VALUES (?, ?)",
            (name, sort_order),
        )
    for term in common.DEFAULT_GENERIC_COMPETENCIES:
        conn.execute(
            "INSERT OR IGNORE INTO skills(normalized, name, bucket, note) "
            "VALUES (?, ?, 'generic', '')",
            (term, term),
        )
    for alias, canonical in common.DEFAULT_ALIASES.items():
        conn.execute(
            "INSERT OR IGNORE INTO skill_aliases(alias, canonical) VALUES (?, ?)",
            (alias, canonical),
        )
    _add_missing_column(conn, "source", "TEXT")
    _add_missing_column(conn, "source_id", "TEXT")
    _add_missing_column(conn, "api_url", "TEXT")
    _add_missing_column(conn, "employer_id", "TEXT")
    _add_missing_column(conn, "injections", "TEXT")
    _add_missing_column(conn, "fetched_ts", "TEXT")


def migrate_4(conn: sqlite3.Connection) -> None:
    """Rebuild vacancies so the status CHECK includes all 12 statuses."""
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        _vacancies_ddl().replace(
            "CREATE TABLE vacancies ", "CREATE TABLE vacancies_new "
        )
    )
    conn.execute("INSERT INTO vacancies_new SELECT * FROM vacancies")
    conn.execute("DROP TABLE vacancies")
    conn.execute("ALTER TABLE vacancies_new RENAME TO vacancies")
    conn.execute("CREATE INDEX idx_vacancies_status ON vacancies(status)")
    conn.execute("PRAGMA foreign_keys=ON")


MIGRATIONS = {1: migrate_1, 2: migrate_2, 3: migrate_3, 4: migrate_4}


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting schema version."""
    cur = conn.execute("PRAGMA user_version").fetchone()[0]
    for n in range(cur + 1, SCHEMA_VERSION + 1):
        MIGRATIONS[n](conn)
        conn.execute(f"PRAGMA user_version = {int(n)}")
        conn.commit()
    return conn.execute("PRAGMA user_version").fetchone()[0]


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating parents as needed) and migrate the state database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn