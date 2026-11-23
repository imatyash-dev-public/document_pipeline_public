"""Skills vault check/add CLI for the document pipeline, backed by SQLite.

``python -m scripts.skills_tool`` prints exactly one compact JSON object.
The skills vault and pending-question queue are stored in the state database
(``skills``, ``skill_aliases``, ``pending_skills``,
``pending_skill_vacancies``). Library functions never read the clock;
``now`` is passed in.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from scripts import common
from scripts.db import connect


BUCKETS = {
    "confirmed": "confirmed_skills",
    "rusty": "rusty_skills",
    "learnable": "learnable_80h_whitelist",
    "missing": "confirmed_missing",
}

CATEGORY = {
    "confirmed_skills": "KNOWN",
    "rusty_skills": "RUSTY_NEEDS_REFRESH",
    "learnable_80h_whitelist": "LEARNABLE_80H",
    "confirmed_missing": "MISSING",
}

VAULT_KEY_TO_BUCKET = {
    "confirmed_skills": "confirmed",
    "rusty_skills": "rusty",
    "learnable_80h_whitelist": "learnable",
    "confirmed_missing": "missing",
    "generic_competencies": "generic",
}

BUCKET_TO_VAULT_KEY = {v: k for k, v in VAULT_KEY_TO_BUCKET.items()}


def load_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{alias: canonical}`` from ``skill_aliases``."""
    rows = conn.execute("SELECT alias, canonical FROM skill_aliases").fetchall()
    return {row["alias"]: row["canonical"] for row in rows}


def get_category_id(conn: sqlite3.Connection, name: str) -> int | None:
    """Return the ``category_id`` for ``name`` or ``None`` when unknown."""
    row = conn.execute(
        "SELECT category_id FROM skill_categories WHERE name=?", (name,)
    ).fetchone()
    return row["category_id"] if row is not None else None


def add_category(
    conn: sqlite3.Connection, name: str, sort_order: int | None = None
) -> dict:
    """Insert a category (or return the existing one) and its summary."""
    name = name.strip()
    if not name:
        raise common.CliError("empty_category", "category name is empty")
    existing = conn.execute(
        "SELECT category_id, sort_order FROM skill_categories WHERE name=?",
        (name,),
    ).fetchone()
    if existing is not None:
        return {
            "ok": True,
            "category_id": existing["category_id"],
            "name": name,
            "sort_order": existing["sort_order"],
        }
    if sort_order is None:
        sort_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM skill_categories"
        ).fetchone()[0] + 1
    cur = conn.execute(
        "INSERT INTO skill_categories(name, sort_order) VALUES (?, ?)",
        (name, sort_order),
    )
    conn.commit()
    return {
        "ok": True,
        "category_id": cur.lastrowid,
        "name": name,
        "sort_order": sort_order,
    }


def load_vault(conn: sqlite3.Connection) -> dict:
    """Build the legacy-shaped vault dict from the DB for the pure helpers."""
    vault: dict = {key: {} for key in VAULT_KEY_TO_BUCKET}
    rows = conn.execute(
        "SELECT s.normalized, s.name, s.bucket, s.note, s.added_ts, "
        "c.name AS category FROM skills s "
        "LEFT JOIN skill_categories c ON c.category_id = s.category_id"
    )
    for row in rows:
        key = BUCKET_TO_VAULT_KEY[row["bucket"]]
        vault[key][row["normalized"]] = {
            "name": row["name"],
            "note": row["note"],
            "added_ts": row["added_ts"],
            "category": row["category"],
        }
    vault[common.ALIASES_KEY] = load_aliases(conn)
    return vault


def list_categories(conn: sqlite3.Connection) -> dict:
    """Return all skill categories ordered by ``sort_order``."""
    rows = conn.execute(
        "SELECT category_id, name, sort_order FROM skill_categories "
        "ORDER BY sort_order"
    ).fetchall()
    return {"categories": [dict(row) for row in rows], "total": len(rows)}


def import_vault(
    conn: sqlite3.Connection, root, category_map: dict | None
) -> dict:
    """Import the legacy JSON vault and pending queue into the DB."""
    cat_lookup: dict[str, int] = {}
    if category_map is not None:
        if not isinstance(category_map, dict):
            raise common.CliError(
                "invalid_map", "category map must be an object"
            )
        for cat_name, items in category_map.items():
            cid = get_category_id(conn, cat_name)
            if cid is None:
                continue
            if not isinstance(items, list):
                raise common.CliError("invalid_map", str(cat_name))
            for s in items:
                if isinstance(s, str):
                    cat_lookup[common.normalize_term(s)] = cid

    vault_path = common.vault_path(root)
    if vault_path.exists():
        data = common.read_json(vault_path)
        if not isinstance(data, dict):
            raise common.CliError("vault_invalid", str(vault_path))
    else:
        data = {}

    skills_imported = 0
    skills_skipped = 0
    for long_key, bucket in VAULT_KEY_TO_BUCKET.items():
        entries = data.get(long_key)
        if entries is None:
            continue
        if long_key == "generic_competencies":
            conn.execute("DELETE FROM skills WHERE bucket='generic'")
        if isinstance(entries, dict):
            items: list[dict] = []
            for norm, e in entries.items():
                if isinstance(e, dict):
                    items.append(
                        {
                            "name": e.get("name", norm),
                            "note": e.get("note", ""),
                            "added_ts": e.get("added_ts"),
                        }
                    )
                elif isinstance(e, str):
                    items.append({"name": e, "note": "", "added_ts": None})
        elif isinstance(entries, list):
            items = [
                {"name": s, "note": "", "added_ts": None}
                for s in entries
                if isinstance(s, str)
            ]
        else:
            raise common.CliError("vault_invalid", long_key)
        for item in items:
            key = common.normalize_term(item["name"])
            existing = conn.execute(
                "SELECT 1 FROM skills WHERE normalized=?", (key,)
            ).fetchone()
            if existing is not None:
                skills_skipped += 1
                continue
            cid = cat_lookup.get(key)
            conn.execute(
                "INSERT INTO skills(normalized, name, bucket, note, added_ts, "
                "category_id) VALUES (?,?,?,?,?,?)",
                (
                    key,
                    item["name"],
                    bucket,
                    item["note"],
                    item["added_ts"],
                    cid,
                ),
            )
            skills_imported += 1

    if category_map is not None:
        for norm_display, cid in cat_lookup.items():
            conn.execute(
                "UPDATE skills SET category_id=? WHERE normalized=?",
                (cid, norm_display),
            )

    aliases_imported = 0
    aliases = data.get(common.ALIASES_KEY)
    if isinstance(aliases, dict):
        for alias, canonical in aliases.items():
            if isinstance(canonical, str):
                conn.execute(
                    "INSERT OR REPLACE INTO skill_aliases(alias, canonical) "
                    "VALUES (?,?)",
                    (
                        common.normalize_term(str(alias)),
                        common.normalize_term(canonical),
                    ),
                )
                aliases_imported += 1

    pending_imported = 0
    pending_path = common.pending_path(root)
    if pending_path.exists():
        pd = common.read_json(pending_path)
        if not isinstance(pd, dict):
            raise common.CliError("pending_invalid", str(pending_path))
    else:
        pd = {}
    for key, e in pd.items():
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        first_seen = e.get("first_seen")
        if not isinstance(name, str) or not isinstance(first_seen, str):
            continue
        nkey = common.normalize_term(name)
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_skills(normalized, name, "
            "first_seen) VALUES (?,?,?)",
            (nkey, name, first_seen),
        )
        if cur.rowcount:
            pending_imported += 1
        vacancies = e.get("vacancies", [])
        if not isinstance(vacancies, list):
            vacancies = []
        for vid in vacancies:
            if not isinstance(vid, str):
                continue
            found = conn.execute(
                "SELECT 1 FROM vacancies WHERE vacancy_id=?", (vid,)
            ).fetchone()
            if found is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO pending_skill_vacancies"
                "(normalized, vacancy_id) VALUES (?,?)",
                (nkey, vid),
            )

    categorized = conn.execute(
        "SELECT COUNT(*) FROM skills WHERE category_id IS NOT NULL"
    ).fetchone()[0]
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM skills WHERE category_id IS NULL "
        "AND bucket <> 'generic'"
    ).fetchone()[0]
    conn.commit()

    return {
        "ok": True,
        "skills_imported": skills_imported,
        "skills_skipped": skills_skipped,
        "aliases_imported": aliases_imported,
        "pending_imported": pending_imported,
        "categorized": categorized,
        "uncategorized": uncategorized,
    }


def delete_pending_for(
    conn: sqlite3.Connection, normalized: str, aliases: dict[str, str]
) -> bool:
    """Delete pending rows for ``normalized`` or any alias resolving to it."""
    keys = [normalized] + sorted(
        {a for a, c in aliases.items() if c == normalized}
    )
    placeholders = ",".join("?" for _ in keys)
    cur = conn.execute(
        f"DELETE FROM pending_skills WHERE normalized IN ({placeholders})",
        keys,
    )
    return cur.rowcount > 0


def parse_skill_list(
    arg: str, aliases: dict[str, str] | None = None
) -> list[str]:
    """Split a comma-separated skill list, deduping after alias collapse."""
    resolved = aliases or {}
    skills: list[str] = []
    seen: set[str] = set()
    for raw in arg.split(","):
        skill = raw.strip()
        if not skill:
            continue
        key = common.apply_alias(skill, resolved)
        if key in seen:
            continue
        seen.add(key)
        skills.append(skill)
    return skills


def classify(vault: dict, skill: str) -> str:
    """Return ``KNOWN``, ``RUSTY_NEEDS_REFRESH``, ``LEARNABLE_80H``, ``MISSING`` or ``UNKNOWN_ASK_USER``."""
    key = common.apply_alias(skill, vault.get(common.ALIASES_KEY, {}))
    for bucket in common.VAULT_KEYS:
        if key in vault.get(bucket, {}):
            return CATEGORY[bucket]
    if key in vault.get(common.GENERIC_KEY, {}):
        return "KNOWN"
    return "UNKNOWN_ASK_USER"


def check_skills(vault: dict, skills: list[str]) -> dict:
    """Classify each skill and list the unknown ones in input order."""
    results: dict = {}
    ask_user: list[str] = []
    for skill in skills:
        category = classify(vault, skill)
        results[skill] = category
        if category == "UNKNOWN_ASK_USER":
            ask_user.append(skill)
    return {"results": results, "ask_user": ask_user}


def audit_skills(vault: dict, skills: list[str]) -> dict:
    """Group ``skills`` by vault bucket without changing the vault."""
    labels = {vault_key: name for name, vault_key in BUCKETS.items()}
    groups: dict = {
        "confirmed": [],
        "rusty": [],
        "learnable": [],
        "missing": [],
        "generic": [],
        "unknown": [],
    }
    for skill in skills:
        key = common.apply_alias(skill, vault.get(common.ALIASES_KEY, {}))
        for bucket in common.VAULT_KEYS:
            entries = vault.get(bucket, {})
            if key not in entries:
                continue
            if bucket == "rusty_skills":
                entry = entries[key]
                groups["rusty"].append(
                    {
                        "skill": skill,
                        "status": CATEGORY[bucket],
                        "canonical": entry.get("name"),
                        "note": entry.get("note"),
                    }
                )
            else:
                groups[labels[bucket]].append(skill)
            break
        else:
            if key in vault.get(common.GENERIC_KEY, {}):
                groups["generic"].append(skill)
            else:
                groups["unknown"].append(skill)
    groups["needs_user_input"] = len(groups["unknown"]) > 0
    return groups


def save_skill(
    conn: sqlite3.Connection,
    name: str,
    bucket: str,
    note: str,
    now: str,
    category_name: str | None = None,
) -> dict:
    """Upsert ``name`` into ``bucket`` and return the change summary."""
    name = name.strip()
    if not name:
        raise common.CliError("empty_skill", "skill name is empty")

    aliases = load_aliases(conn)
    key = common.apply_alias(name, aliases)
    target = BUCKETS[bucket]

    row = conn.execute(
        "SELECT bucket FROM skills WHERE normalized=?", (key,)
    ).fetchone()
    if row is not None and row["bucket"] != bucket:
        moved_from = BUCKET_TO_VAULT_KEY[row["bucket"]]
    else:
        moved_from = None

    conn.execute(
        "INSERT INTO skills(normalized,name,bucket,note,added_ts) "
        "VALUES (?,?,?,?,?) ON CONFLICT(normalized) DO UPDATE SET "
        "name=excluded.name, bucket=excluded.bucket, note=excluded.note, "
        "added_ts=excluded.added_ts",
        (key, name, bucket, note, now),
    )
    if category_name is not None:
        cid = get_category_id(conn, category_name)
        if cid is None:
            raise common.CliError("category_not_found", category_name)
        conn.execute(
            "UPDATE skills SET category_id=? WHERE normalized=?", (cid, key)
        )
    eff = conn.execute(
        "SELECT c.name FROM skills s "
        "LEFT JOIN skill_categories c ON c.category_id = s.category_id "
        "WHERE s.normalized=?",
        (key,),
    ).fetchone()
    eff_name = eff["name"] if eff else None
    cleared_pending = delete_pending_for(conn, key, aliases)
    conn.commit()

    return {
        "ok": True,
        "skill": name,
        "bucket": target,
        "category": CATEGORY[target],
        "category_name": eff_name,
        "moved_from": moved_from,
        "cleared_pending": cleared_pending,
    }


def set_category(
    conn: sqlite3.Connection, skill_name: str, category_name: str
) -> dict:
    """Assign ``category_name`` to an existing skill."""
    name = skill_name.strip()
    if not name:
        raise common.CliError("empty_skill", "skill name is empty")
    aliases = load_aliases(conn)
    key = common.apply_alias(name, aliases)
    found = conn.execute(
        "SELECT 1 FROM skills WHERE normalized=?", (key,)
    ).fetchone()
    if found is None:
        raise common.CliError("skill_not_found", skill_name)
    cid = get_category_id(conn, category_name)
    if cid is None:
        raise common.CliError("category_not_found", category_name)
    conn.execute(
        "UPDATE skills SET category_id=? WHERE normalized=?", (cid, key)
    )
    conn.commit()
    return {"ok": True, "skill": name, "category": category_name}


def list_uncategorized(conn: sqlite3.Connection) -> dict:
    """Return skills that have no category assigned."""
    rows = conn.execute(
        "SELECT name, bucket FROM skills WHERE category_id IS NULL "
        "AND bucket <> 'generic' ORDER BY bucket, name"
    ).fetchall()
    return {
        "skills": [{"name": r["name"], "bucket": r["bucket"]} for r in rows],
        "total": len(rows),
    }


def list_pending(conn: sqlite3.Connection) -> dict:
    """Return the pending queue as a key-sorted list of entries."""
    items: list[dict] = []
    rows = conn.execute(
        "SELECT normalized, name, first_seen FROM pending_skills "
        "ORDER BY normalized"
    ).fetchall()
    for row in rows:
        vacancies = [
            r["vacancy_id"]
            for r in conn.execute(
                "SELECT vacancy_id FROM pending_skill_vacancies "
                "WHERE normalized=? ORDER BY vacancy_id",
                (row["normalized"],),
            )
        ]
        items.append(
            {
                "term": row["name"],
                "key": row["normalized"],
                "vacancies": vacancies,
                "first_seen": row["first_seen"],
            }
        )
    return {"pending": items, "total": len(items)}


def add_pending(
    conn: sqlite3.Connection,
    skills: list[str],
    vacancy_id: str,
    now: str,
) -> dict:
    """Register ``skills`` for ``vacancy_id``, keyed by canonical term."""
    found = conn.execute(
        "SELECT 1 FROM vacancies WHERE vacancy_id=?", (vacancy_id,)
    ).fetchone()
    if found is None:
        raise common.CliError("vacancy_not_found", vacancy_id)

    aliases = load_aliases(conn)
    added: list[str] = []
    already: list[str] = []
    for skill in skills:
        name = skill.strip()
        if not name:
            continue
        key = common.apply_alias(name, aliases)
        existing = conn.execute(
            "SELECT 1 FROM pending_skills WHERE normalized=?", (key,)
        ).fetchone()
        if existing is not None:
            already.append(name)
        else:
            conn.execute(
                "INSERT INTO pending_skills(normalized,name,first_seen) "
                "VALUES (?,?,?)",
                (key, name, now),
            )
            added.append(name)
        conn.execute(
            "INSERT OR IGNORE INTO pending_skill_vacancies"
            "(normalized,vacancy_id) VALUES (?,?)",
            (key, vacancy_id),
        )
    conn.commit()
    return {"added": added, "already_pending": already}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the CLI handler, returning the exit code."""
    parser = argparse.ArgumentParser(prog="skills_tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check")
    group.add_argument("--add")
    group.add_argument("--audit")
    group.add_argument("--pending")
    group.add_argument("--list-pending", action="store_true")
    group.add_argument("--list-categories", action="store_true")
    group.add_argument("--add-category", metavar="NAME")
    group.add_argument("--set-category", metavar="SKILL")
    group.add_argument("--list-uncategorized", action="store_true")
    group.add_argument("--import-vault", action="store_true")
    group.add_argument(
        "--list-bucket",
        choices=["confirmed", "rusty", "learnable", "missing"],
    )
    parser.add_argument(
        "--bucket", choices=["confirmed", "rusty", "learnable", "missing"]
    )
    parser.add_argument("--note", default="")
    parser.add_argument("--vacancy", default=None)
    parser.add_argument("--category", default=None)
    parser.add_argument("--sort", type=int, default=None)
    parser.add_argument("--map", default=None)
    common.add_common_args(parser, db=True)

    args = parser.parse_args(argv)
    if args.add and not args.bucket:
        parser.error("--add requires --bucket")
    if args.set_category is not None and not args.category:
        parser.error("--set-category requires --category")
    if args.vacancy is not None and args.pending is None:
        parser.error("--vacancy requires --pending")
    if args.pending is not None and not args.vacancy:
        parser.error("--pending requires --vacancy")

    root = common.resolve_root(args)
    db_path = common.resolve_db(args)
    conn = connect(db_path)

    def handler() -> dict:
        if args.import_vault:
            cm = common.read_json(Path(args.map)) if args.map else None
            return import_vault(conn, root, cm)
        if args.list_categories:
            return list_categories(conn)
        if args.list_uncategorized:
            return list_uncategorized(conn)
        if args.add_category is not None:
            return add_category(conn, args.add_category, args.sort)
        if args.set_category is not None:
            return set_category(conn, args.set_category, args.category)
        if args.list_pending:
            return list_pending(conn)
        if args.pending is not None:
            now = common.resolve_now(args)
            skills = parse_skill_list(args.pending, load_aliases(conn))
            res = add_pending(conn, skills, args.vacancy, now)
            total = conn.execute(
                "SELECT COUNT(*) FROM pending_skills"
            ).fetchone()[0]
            return {
                "ok": True,
                "added": res["added"],
                "already_pending": res["already_pending"],
                "total_pending": total,
            }
        if args.check is not None:
            vault = load_vault(conn)
            return check_skills(
                vault, parse_skill_list(args.check, vault[common.ALIASES_KEY])
            )
        if args.audit is not None:
            vault = load_vault(conn)
            return audit_skills(
                vault, parse_skill_list(args.audit, vault[common.ALIASES_KEY])
            )
        if args.list_bucket is not None:
            vault = load_vault(conn)
            bucket = BUCKETS[args.list_bucket]
            skills = list(vault.get(bucket, {}).values())
            return {
                "bucket": bucket,
                "total": len(skills),
                "skills": skills,
            }
        now = common.resolve_now(args)
        return save_skill(
            conn, args.add, args.bucket, args.note, now,
            category_name=args.category,
        )

    try:
        return common.run(handler)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
