"""Pipeline state and URL-cache manager CLI for the document pipeline.

``python -m scripts.pipeline_ctl`` prints exactly one compact JSON object.
Library functions never read the clock; ``now`` is passed in.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime, time

from scripts import common
from scripts.db import TERMINAL_STATUSES, VACANCY_STATUSES, connect


MAX_QA_ATTEMPTS = 2

ACTIONS = {
    "DISCOVERED": "SCORE_MATCH",
    "OSINT_READY": "BUILD_PLAN",
    "AWAITING_SKILL_CLARIFICATION": "ASK_USER_SKILLS",
    "PLAN_READY": "WRITE_DRAFT",
    "DRAFT_READY": "RUN_QA",
    "QA_FAILED_WRITER": "REWRITE_DRAFT",
    "QA_FAILED_STRATEGY": "REBUILD_PLAN",
}

PRIORITY = {
    "QA_FAILED_STRATEGY": 1,
    "QA_FAILED_WRITER": 2,
    "DRAFT_READY": 3,
    "PLAN_READY": 4,
    "OSINT_READY": 5,
    "DISCOVERED": 6,
    "AWAITING_SKILL_CLARIFICATION": 7,
}

PEAK_WINDOWS: tuple[tuple[time, time, str], ...] = (
    (time(0, 45), time(4, 0), "00:45-04:00Z"),
    (time(5, 45), time(10, 0), "05:45-10:00Z"),
)


def check_peak_gate(now: datetime) -> dict:
    """Report whether ``now`` falls inside a DeepSeek peak-cost window."""
    if now.weekday() >= 5:
        return {"result": "GATE_OK", "window": None, "opens_at": None}
    hhmm = now.time()
    for start, end, label in PEAK_WINDOWS:
        if start <= hhmm < end:
            opens = now.replace(
                hour=end.hour, minute=end.minute, second=0, microsecond=0
            )
            return {
                "result": "GATE_BLOCKED",
                "window": label,
                "opens_at": opens.strftime(common.TS_FORMAT),
            }
    return {"result": "GATE_OK", "window": None, "opens_at": None}


def init_state(conn: sqlite3.Connection, root: Path) -> dict:
    """Report the schema version and the seeded skill-category count."""
    return {
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "categories": conn.execute(
            "SELECT COUNT(*) FROM skill_categories"
        ).fetchone()[0],
    }


def check_url(conn: sqlite3.Connection, url: str, now: str) -> dict:
    """Report whether the URL may be scraped or must be skipped from cache."""
    row = conn.execute(
        "SELECT last_scraped_ts FROM url_cache WHERE url_hash = ?",
        (common.url_hash(url),),
    ).fetchone()
    if row is None:
        return {"result": "ALLOW_SCRAPE", "last_scraped_ts": None, "age_days": None}
    ts = row["last_scraped_ts"]
    result = "CACHE_SKIP" if common.is_fresh(ts, now) else "ALLOW_SCRAPE"
    return {
        "result": result,
        "last_scraped_ts": ts,
        "age_days": round(common.days_between(ts, now), 2),
    }


def record_url(
    conn: sqlite3.Connection,
    url: str,
    now: str,
    status_code: int | None,
) -> str:
    """Upsert the URL cache row and return the URL hash."""
    digest = common.url_hash(url)
    conn.execute(
        "INSERT INTO url_cache(url_hash,url,last_scraped_ts,status_code) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(url_hash) DO UPDATE SET "
        "url=excluded.url, "
        "last_scraped_ts=excluded.last_scraped_ts, "
        "status_code=excluded.status_code",
        (digest, url, now, status_code),
    )
    conn.commit()
    return digest


def upsert_company(
    conn: sqlite3.Connection,
    slug: str,
    name: str,
    domain: str | None,
) -> None:
    """Insert or update a company row, keeping an existing domain."""
    common.validate_slug(slug)
    conn.execute(
        "INSERT INTO companies(company_slug,company_name,domain) VALUES (?,?,?) "
        "ON CONFLICT(company_slug) DO UPDATE SET "
        "company_name=excluded.company_name, "
        "domain=COALESCE(excluded.domain, companies.domain)",
        (slug, name, domain),
    )
    conn.commit()


def set_company_osint(
    conn: sqlite3.Connection,
    slug: str,
    dossier_path: str,
    now: str,
) -> None:
    """Record a company's dossier path and OSINT timestamp."""
    common.validate_slug(slug)
    cur = conn.execute(
        "UPDATE companies SET dossier_path=?, last_osint_ts=? "
        "WHERE company_slug=?",
        (dossier_path, now, slug),
    )
    if cur.rowcount == 0:
        raise common.CliError("company_not_found", slug)
    conn.commit()


def upsert_vacancy(
    conn: sqlite3.Connection,
    vacancy_id: str,
    company_slug: str,
    title: str,
    url: str | None,
    folder_path: str,
    now: str,
) -> bool:
    """Insert a new vacancy or refresh an existing one; return True if created."""
    common.validate_vacancy_id(vacancy_id)
    common.validate_slug(company_slug)
    row = conn.execute(
        "SELECT 1 FROM companies WHERE company_slug=?",
        (company_slug,),
    ).fetchone()
    if row is None:
        upsert_company(conn, company_slug, company_slug, None)
    row = conn.execute(
        "SELECT 1 FROM vacancies WHERE vacancy_id=?",
        (vacancy_id,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE vacancies SET title=?, url=?, folder_path=?, updated_ts=? "
            "WHERE vacancy_id=?",
            (title, url, folder_path, now, vacancy_id),
        )
        conn.commit()
        return False
    conn.execute(
        "INSERT INTO vacancies(vacancy_id,company_slug,title,url,status,"
        "qa_attempts,folder_path,created_ts,updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            vacancy_id,
            company_slug,
            title,
            url,
            "DISCOVERED",
            0,
            folder_path,
            now,
            now,
        ),
    )
    conn.commit()
    return True


def update_vacancy(
    conn: sqlite3.Connection,
    vacancy_id: str,
    status: str,
    now: str,
    score: int | None = None,
) -> dict:
    """Update a vacancy status (and optional score), returning its new state."""
    if status not in VACANCY_STATUSES:
        raise common.CliError("invalid_status", status)
    if score is not None and not 0 <= score <= 100:
        raise common.CliError("invalid_score", str(score))
    row = conn.execute(
        "SELECT qa_attempts FROM vacancies WHERE vacancy_id=?",
        (vacancy_id,),
    ).fetchone()
    if row is None:
        raise common.CliError("vacancy_not_found", vacancy_id)
    qa_attempts = row["qa_attempts"]
    if status.startswith("QA_FAILED"):
        qa_attempts += 1
    if score is not None:
        conn.execute(
            "UPDATE vacancies SET status=?, qa_attempts=?, match_score=?, "
            "updated_ts=? WHERE vacancy_id=?",
            (status, qa_attempts, score, now, vacancy_id),
        )
    else:
        conn.execute(
            "UPDATE vacancies SET status=?, qa_attempts=?, updated_ts=? "
            "WHERE vacancy_id=?",
            (status, qa_attempts, now, vacancy_id),
        )
    conn.commit()
    row = conn.execute(
        "SELECT status, qa_attempts, match_score FROM vacancies "
        "WHERE vacancy_id=?",
        (vacancy_id,),
    ).fetchone()
    return {
        "vacancy_id": vacancy_id,
        "status": row["status"],
        "qa_attempts": row["qa_attempts"],
        "match_score": row["match_score"],
    }


def set_vacancy_details(
    conn: sqlite3.Connection,
    vacancy_id: str,
    area: str | None,
    experience: str | None,
    salary: dict | None,
    hard_skills: list[str],
    employer_url: str | None,
    description: str,
    now: str,
    source: str | None = None,
    source_id: str | None = None,
    api_url: str | None = None,
    employer_id: str | None = None,
    injections: list | None = None,
    fetched_ts: str | None = None,
) -> None:
    """Store structured vacancy metadata copied from the discovery file."""
    salary_json = (
        json.dumps(salary, ensure_ascii=False, separators=(",", ":"))
        if salary is not None
        else None
    )
    hard_skills_json = json.dumps(
        hard_skills, ensure_ascii=False, separators=(",", ":")
    )
    injections_json = (
        json.dumps(injections, ensure_ascii=False, separators=(",", ":"))
        if injections is not None
        else None
    )
    cur = conn.execute(
        "UPDATE vacancies SET area=?, experience=?, salary=?, hard_skills=?, "
        "employer_url=?, description=?, source=?, source_id=?, api_url=?, "
        "employer_id=?, injections=?, fetched_ts=?, updated_ts=? "
        "WHERE vacancy_id=?",
        (
            area,
            experience,
            salary_json,
            hard_skills_json,
            employer_url,
            description,
            source,
            source_id,
            api_url,
            employer_id,
            injections_json,
            fetched_ts,
            now,
            vacancy_id,
        ),
    )
    if cur.rowcount == 0:
        raise common.CliError("vacancy_not_found", vacancy_id)
    conn.commit()


def backfill_details(conn: sqlite3.Connection, root: Path) -> dict:
    """Populate the detail columns from each vacancy's clean JSON file."""
    filled = 0
    skipped_missing: list[str] = []
    skipped_invalid: list[str] = []
    pattern = "companies/*/vacancies/*/1_vacancy_clean.json"
    for path in sorted(root.glob(pattern)):
        try:
            rec = common.read_json(path)
        except common.CliError:
            skipped_invalid.append(common.rel_path(path, root))
            continue
        if not isinstance(rec, dict):
            skipped_invalid.append(common.rel_path(path, root))
            continue
        vid = rec.get("vacancy_id")
        if not isinstance(vid, str) or not vid:
            skipped_invalid.append(common.rel_path(path, root))
            continue
        row = conn.execute(
            "SELECT 1 FROM vacancies WHERE vacancy_id=?",
            (vid,),
        ).fetchone()
        if row is None:
            skipped_missing.append(vid)
            continue
        hard_skills = rec.get("hard_skills")
        if not isinstance(hard_skills, list):
            hard_skills = []
        salary = rec.get("salary")
        if not isinstance(salary, dict):
            salary = None
        injections = rec.get("injections")
        if not isinstance(injections, list):
            injections = None
        conn.execute(
            "UPDATE vacancies SET area=?, experience=?, salary=?, "
            "hard_skills=?, employer_url=?, description=?, source=?, "
            "source_id=?, api_url=?, employer_id=?, injections=?, "
            "fetched_ts=? WHERE vacancy_id=?",
            (
                rec.get("area"),
                rec.get("experience"),
                (
                    json.dumps(
                        salary, ensure_ascii=False, separators=(",", ":")
                    )
                    if salary is not None
                    else None
                ),
                json.dumps(
                    hard_skills, ensure_ascii=False, separators=(",", ":")
                ),
                rec.get("employer_url"),
                rec.get("description"),
                rec.get("source"),
                rec.get("source_id"),
                rec.get("api_url"),
                rec.get("employer_id"),
                (
                    json.dumps(
                        injections, ensure_ascii=False, separators=(",", ":")
                    )
                    if injections is not None
                    else None
                ),
                rec.get("fetched_ts"),
                vid,
            ),
        )
        filled += 1
    conn.commit()
    return {
        "ok": True,
        "filled": filled,
        "skipped_missing": skipped_missing,
        "skipped_invalid": skipped_invalid,
    }


def get_vacancy(conn: sqlite3.Connection, vacancy_id: str) -> dict:
    """Return all stored vacancy fields with JSON columns parsed."""
    row = conn.execute(
        "SELECT v.*, c.company_name FROM vacancies v "
        "JOIN companies c ON c.company_slug = v.company_slug "
        "WHERE v.vacancy_id=?",
        (vacancy_id,),
    ).fetchone()
    if row is None:
        raise common.CliError("vacancy_not_found", vacancy_id)
    data = dict(row)
    salary = (
        json.loads(data["salary"]) if data["salary"] is not None else None
    )
    hard_skills = (
        json.loads(data["hard_skills"])
        if data["hard_skills"] is not None
        else []
    )
    injections = (
        json.loads(data["injections"])
        if data["injections"] is not None
        else []
    )
    return {
        "vacancy_id": data["vacancy_id"],
        "company_slug": data["company_slug"],
        "company_name": data.get("company_name"),
        "title": data["title"],
        "url": data["url"],
        "status": data["status"],
        "match_score": data["match_score"],
        "qa_attempts": data["qa_attempts"],
        "wave": data["wave"],
        "result": data["result"],
        "folder_path": data["folder_path"],
        "created_ts": data["created_ts"],
        "updated_ts": data["updated_ts"],
        "area": data["area"],
        "experience": data["experience"],
        "salary": salary,
        "hard_skills": hard_skills,
        "employer_url": data["employer_url"],
        "description": data["description"],
        "source": data["source"],
        "source_id": data["source_id"],
        "api_url": data["api_url"],
        "employer_id": data["employer_id"],
        "injections": injections,
        "fetched_ts": data["fetched_ts"],
    }


def set_wave(
    conn: sqlite3.Connection,
    vacancy_id: str,
    wave: int,
    now: str,
) -> dict:
    """Record the 3-wave dispatch batch for a vacancy."""
    if (
        not isinstance(wave, int)
        or isinstance(wave, bool)
        or wave not in (1, 2, 3)
    ):
        raise common.CliError("invalid_wave", str(wave))
    cur = conn.execute(
        "UPDATE vacancies SET wave=?, updated_ts=? WHERE vacancy_id=?",
        (wave, now, vacancy_id),
    )
    if cur.rowcount == 0:
        raise common.CliError("vacancy_not_found", vacancy_id)
    conn.commit()
    return {"ok": True, "vacancy_id": vacancy_id, "wave": wave}


def set_result(
    conn: sqlite3.Connection,
    vacancy_id: str,
    result: str,
    now: str,
) -> dict:
    """Record a free-form application outcome for a vacancy."""
    cur = conn.execute(
        "UPDATE vacancies SET result=?, updated_ts=? WHERE vacancy_id=?",
        (result, now, vacancy_id),
    )
    if cur.rowcount == 0:
        raise common.CliError("vacancy_not_found", vacancy_id)
    conn.commit()
    return {"ok": True, "vacancy_id": vacancy_id, "result": result}


def check_company(
    conn: sqlite3.Connection,
    root: Path,
    slug: str,
    now: str,
) -> dict:
    """Report whether a company has a usable, fresh OSINT dossier."""
    common.validate_slug(slug)
    row = conn.execute(
        "SELECT dossier_path, last_osint_ts FROM companies "
        "WHERE company_slug=?",
        (slug,),
    ).fetchone()
    if row is None:
        return {"result": "NEEDS_OSINT", "reason": "no_company"}
    dossier_path = row["dossier_path"]
    last_osint_ts = row["last_osint_ts"]
    if (
        dossier_path is None
        or last_osint_ts is None
        or not (root / dossier_path).is_file()
    ):
        return {"result": "NEEDS_OSINT", "reason": "no_dossier"}
    if not common.is_fresh(last_osint_ts, now):
        return {"result": "NEEDS_OSINT", "reason": "expired"}
    return {
        "result": "CACHE_HIT",
        "dossier_path": dossier_path,
        "age_days": round(common.days_between(last_osint_ts, now), 2),
    }


def halt_exhausted(conn: sqlite3.Connection, now: str) -> list[str]:
    """Halt vacancies that exhausted their QA retries; return their ids."""
    rows = conn.execute(
        "SELECT vacancy_id FROM vacancies "
        "WHERE status IN ('QA_FAILED_WRITER','QA_FAILED_STRATEGY') "
        "AND qa_attempts >= ? "
        "ORDER BY vacancy_id",
        (MAX_QA_ATTEMPTS,),
    ).fetchall()
    ids = [row["vacancy_id"] for row in rows]
    if ids:
        conn.execute(
            "UPDATE vacancies SET status='HALTED_MAX_RETRIES', updated_ts=? "
            "WHERE vacancy_id IN (SELECT vacancy_id FROM vacancies "
            "WHERE status IN ('QA_FAILED_WRITER','QA_FAILED_STRATEGY') "
            "AND qa_attempts >= ?)",
            (now, MAX_QA_ATTEMPTS),
        )
        conn.commit()
    return ids


def next_action(
    conn: sqlite3.Connection,
    root: Path,
    now: str,
) -> dict:
    """Return the next pipeline action, halting exhausted vacancies first."""
    halted = halt_exhausted(conn, now)
    placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        "SELECT vacancy_id, company_slug, status, qa_attempts, match_score, "
        "folder_path, created_ts FROM vacancies "
        f"WHERE status NOT IN ({placeholders})",
        TERMINAL_STATUSES,
    ).fetchall()
    if not rows:
        return {"action": "IDLE", "remaining": 0, "halted": halted}
    ordered = sorted(
        rows,
        key=lambda r: (
            PRIORITY[r["status"]],
            r["created_ts"],
            r["vacancy_id"],
        ),
    )
    r = ordered[0]
    status = r["status"]
    action = ACTIONS[status]
    result: dict = {
        "action": action,
        "vacancy_id": r["vacancy_id"],
        "company_slug": r["company_slug"],
        "status": status,
        "qa_attempts": r["qa_attempts"],
        "match_score": r["match_score"],
        "folder_path": r["folder_path"],
    }
    if status == "DISCOVERED" and r["match_score"] is not None:
        company = check_company(conn, root, r["company_slug"], now)
        if company["result"] == "CACHE_HIT":
            result["action"] = "REUSE_OSINT"
            result["dossier_path"] = company["dossier_path"]
        else:
            result["action"] = "RUN_OSINT"
    result["remaining"] = len(ordered)
    result["halted"] = halted
    return result


def summary(conn: sqlite3.Connection) -> dict:
    """Summarize the vacancy table: totals, status counts and ordered rows."""
    total = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
    counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM vacancies GROUP BY status"
        ).fetchall()
    }
    by_status = {
        status: counts[status]
        for status in VACANCY_STATUSES
        if counts.get(status, 0)
    }
    rows = [
        [
            row["vacancy_id"],
            row["company_slug"],
            row["status"],
            row["match_score"],
            row["qa_attempts"],
            row["wave"],
        ]
        for row in conn.execute(
            "SELECT vacancy_id, company_slug, status, match_score, qa_attempts, "
            "wave FROM vacancies ORDER BY company_slug, vacancy_id"
        ).fetchall()
    ]
    return {
        "total": total,
        "by_status": by_status,
        "cols": [
            "vacancy_id",
            "company_slug",
            "status",
            "match_score",
            "qa_attempts",
            "wave",
        ],
        "rows": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the pipeline state CLI."""
    parser = argparse.ArgumentParser(prog="pipeline_ctl")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true")
    group.add_argument("--gate-check", action="store_true")
    group.add_argument("--check-url")
    group.add_argument("--mark-url")
    group.add_argument("--check-company")
    group.add_argument("--add-vacancy")
    group.add_argument("--update-vacancy")
    group.add_argument("--next", action="store_true")
    group.add_argument("--summary", action="store_true")
    group.add_argument("--get-vacancy")
    group.add_argument("--set-wave")
    group.add_argument("--set-result")
    group.add_argument("--backfill-details", action="store_true")

    parser.add_argument("--status")
    parser.add_argument("--score", type=int)
    parser.add_argument("--wave", type=int)
    parser.add_argument("--result")
    parser.add_argument("--status-code", type=int, dest="status_code")
    parser.add_argument("--company")
    parser.add_argument("--title")
    parser.add_argument("--url")

    common.add_common_args(parser)
    return parser


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict:
    """Resolve paths and dispatch the requested action."""
    root = common.resolve_root(args)
    db_path = common.resolve_db(args)
    now = common.resolve_now(args)
    if args.gate_check:
        return check_peak_gate(common.parse_ts(now))
    conn = connect(db_path)
    try:
        if args.backfill_details:
            return backfill_details(conn, root)
        if args.init:
            state = init_state(conn, root)
            return {
                "ok": True,
                "db": common.rel_path(db_path, root),
                "schema_version": state["schema_version"],
                "categories": state["categories"],
            }
        if args.check_url:
            return check_url(conn, args.check_url, now)
        if args.mark_url:
            digest = record_url(conn, args.mark_url, now, args.status_code)
            return {"ok": True, "url_hash": digest, "last_scraped_ts": now}
        if args.check_company:
            return check_company(conn, root, args.check_company, now)
        if args.add_vacancy:
            if not args.company or not args.title:
                parser.error("--add-vacancy requires --company and --title")
            folder_path = (
                f"companies/{args.company}/vacancies/{args.add_vacancy}"
            )
            created = upsert_vacancy(
                conn,
                args.add_vacancy,
                args.company,
                args.title,
                args.url,
                folder_path,
                now,
            )
            row = conn.execute(
                "SELECT status FROM vacancies WHERE vacancy_id=?",
                (args.add_vacancy,),
            ).fetchone()
            return {
                "ok": True,
                "vacancy_id": args.add_vacancy,
                "created": created,
                "status": row["status"],
            }
        if args.update_vacancy:
            if not args.status:
                parser.error("--update-vacancy requires --status")
            updated = update_vacancy(
                conn,
                args.update_vacancy,
                args.status,
                now,
                args.score,
            )
            return {
                "ok": True,
                "vacancy_id": updated["vacancy_id"],
                "status": updated["status"],
                "qa_attempts": updated["qa_attempts"],
                "match_score": updated["match_score"],
            }
        if args.next:
            return next_action(conn, root, now)
        if args.summary:
            return summary(conn)
        if args.get_vacancy:
            return get_vacancy(conn, args.get_vacancy)
        if args.set_wave:
            if args.wave is None:
                parser.error("--set-wave requires --wave")
            return set_wave(conn, args.set_wave, args.wave, now)
        if args.set_result:
            if args.result is None:
                parser.error("--set-result requires --result")
            return set_result(conn, args.set_result, args.result, now)
        raise common.CliError("not_implemented", "action")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the CLI handler, returning the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return common.run(lambda: handle(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())