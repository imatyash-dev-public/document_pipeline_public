"""CLI tests for scripts.skills_tool DB-backed check/add behaviour.

Every test uses a fresh SQLite state DB under pytest's ``tmp_path``; the
repository's real ``master_data/`` is never read or touched. Skill text is
Cyrillic, so a UTF-8 regression in argument handling or DB storage surfaces
here.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-01-01T00:00:00Z"


def cli(tool, tmp_path, *args, now=NOW):
    """Run ``python -m scripts.<tool>`` against a temp root and return it."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            f"scripts.{tool}",
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


def init_db(tmp_path):
    """Create and migrate the state DB via pipeline_ctl --init."""
    result = cli("pipeline_ctl", tmp_path, "--init")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def open_db(tmp_path):
    """Open the temp state DB for direct row assertions."""
    conn = sqlite3.connect(tmp_path / "db" / "state.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def test_check_unknown_then_add_known(tmp_path):
    """An unknown skill is asked about, then becomes KNOWN once confirmed."""
    init_db(tmp_path)

    result = cli("skills_tool", tmp_path, "--check", "Python")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"Python": "UNKNOWN_ASK_USER"},
        "ask_user": ["Python"],
    }

    result = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Python",
        "--bucket",
        "confirmed",
        "--note",
        "Опыт 5 лет",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "skill": "Python",
        "bucket": "confirmed_skills",
        "category": "KNOWN",
        "category_name": None,
        "moved_from": None,
        "cleared_pending": False,
    }

    result = cli("skills_tool", tmp_path, "--check", "Python")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"Python": "KNOWN"},
        "ask_user": [],
    }


def test_learnable_and_missing(tmp_path):
    """Learnable and missing buckets classify correctly and keep Cyrillic."""
    init_db(tmp_path)

    result = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Kubernetes",
        "--bucket",
        "learnable",
        "--note",
        "Можно освоить за 80 часов",
    )
    assert result.returncode == 0, result.stderr

    result = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "1С:Бухгалтерия",
        "--bucket",
        "missing",
    )
    assert result.returncode == 0, result.stderr

    result = cli(
        "skills_tool",
        tmp_path,
        "--check",
        "Kubernetes,1С:Бухгалтерия",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {
            "Kubernetes": "LEARNABLE_80H",
            "1С:Бухгалтерия": "MISSING",
        },
        "ask_user": [],
    }

    conn = open_db(tmp_path)
    entry = conn.execute(
        "SELECT note, added_ts FROM skills WHERE normalized=?",
        ("kubernetes",),
    ).fetchone()
    conn.close()
    assert entry["note"] == "Можно освоить за 80 часов"
    assert entry["added_ts"] == NOW


def test_add_moves_between_buckets(tmp_path):
    """Re-adding a skill moves its key and reports the previous bucket."""
    init_db(tmp_path)

    first = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Go",
        "--bucket",
        "learnable",
        "--note",
        "Можно освоить за 80 часов",
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["moved_from"] is None

    second = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Go",
        "--bucket",
        "confirmed",
        "--note",
        "Практический опыт",
    )
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["moved_from"] == "learnable_80h_whitelist"
    assert payload["bucket"] == "confirmed_skills"

    conn = open_db(tmp_path)
    bucket = conn.execute(
        "SELECT bucket FROM skills WHERE normalized=?", ("go",)
    ).fetchone()["bucket"]
    learnable = conn.execute(
        "SELECT COUNT(*) FROM skills WHERE bucket='learnable'"
    ).fetchone()[0]
    conn.close()
    assert bucket == "confirmed"
    assert learnable == 0


def test_case_and_yo_insensitive(tmp_path):
    """Case, doubled spaces and yo/e are normalized when matching."""
    init_db(tmp_path)

    result = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Ёмкостное планирование",
        "--bucket",
        "confirmed",
    )
    assert result.returncode == 0, result.stderr

    result = cli(
        "skills_tool",
        tmp_path,
        "--check",
        "ёмкостное  ПЛАНИРОВАНИЕ",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["results"] == {"ёмкостное  ПЛАНИРОВАНИЕ": "KNOWN"}
    assert payload["ask_user"] == []


def test_add_rusty_bucket_and_check(tmp_path):
    """A rusty skill is stored with its note and never asks the user."""
    init_db(tmp_path)

    result = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "SOAP",
        "--bucket",
        "rusty",
        "--note",
        "Освежить перед интервью",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "skill": "SOAP",
        "bucket": "rusty_skills",
        "category": "RUSTY_NEEDS_REFRESH",
        "category_name": None,
        "moved_from": None,
        "cleared_pending": False,
    }

    result = cli("skills_tool", tmp_path, "--check", "SOAP")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"SOAP": "RUSTY_NEEDS_REFRESH"},
        "ask_user": [],
    }

    conn = open_db(tmp_path)
    entry = conn.execute(
        "SELECT bucket, note FROM skills WHERE normalized=?", ("soap",)
    ).fetchone()
    conn.close()
    assert entry["bucket"] == "rusty"
    assert entry["note"] == "Освежить перед интервью"


def test_add_rusty_moves_out_of_confirmed(tmp_path):
    """Promoting to rusty removes the key from confirmed (exclusive buckets)."""
    init_db(tmp_path)

    first = cli(
        "skills_tool", tmp_path, "--add", "SQL", "--bucket", "confirmed"
    )
    assert first.returncode == 0, first.stderr

    second = cli("skills_tool", tmp_path, "--add", "SQL", "--bucket", "rusty")
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["moved_from"] == "confirmed_skills"
    assert payload["bucket"] == "rusty_skills"

    result = cli("skills_tool", tmp_path, "--check", "SQL")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"SQL": "RUSTY_NEEDS_REFRESH"},
        "ask_user": [],
    }

    conn = open_db(tmp_path)
    bucket = conn.execute(
        "SELECT bucket FROM skills WHERE normalized=?", ("sql",)
    ).fetchone()["bucket"]
    confirmed = conn.execute(
        "SELECT COUNT(*) FROM skills WHERE bucket='confirmed'"
    ).fetchone()[0]
    conn.close()
    assert bucket == "rusty"
    assert confirmed == 0


def test_audit_groups_all_buckets(tmp_path):
    """--audit groups every skill into its bucket in a single read-only pass."""
    init_db(tmp_path)

    sql = cli("skills_tool", tmp_path, "--add", "SQL", "--bucket", "confirmed")
    assert sql.returncode == 0, sql.stderr
    soap = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "SOAP",
        "--bucket",
        "rusty",
        "--note",
        "Освежить перед интервью",
    )
    assert soap.returncode == 0, soap.stderr
    go = cli("skills_tool", tmp_path, "--add", "Go", "--bucket", "learnable")
    assert go.returncode == 0, go.stderr
    k8s = cli(
        "skills_tool", tmp_path, "--add", "Kubernetes", "--bucket", "missing"
    )
    assert k8s.returncode == 0, k8s.stderr

    result = cli(
        "skills_tool", tmp_path, "--audit", "Kubernetes,SOAP,REST,SQL,Go"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "confirmed": ["SQL"],
        "rusty": [
            {
                "skill": "SOAP",
                "status": "RUSTY_NEEDS_REFRESH",
                "canonical": "SOAP",
                "note": "Освежить перед интервью",
            }
        ],
        "learnable": ["Go"],
        "missing": ["Kubernetes"],
        "generic": [],
        "unknown": ["REST"],
        "needs_user_input": True,
    }


def test_audit_rusty_only_needs_no_user_input(tmp_path):
    """A rusty-only audit does not ask the user, even without a note."""
    init_db(tmp_path)

    added = cli("skills_tool", tmp_path, "--add", "XML", "--bucket", "rusty")
    assert added.returncode == 0, added.stderr

    result = cli("skills_tool", tmp_path, "--audit", "XML")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "confirmed": [],
        "rusty": [
            {
                "skill": "XML",
                "status": "RUSTY_NEEDS_REFRESH",
                "canonical": "XML",
                "note": "",
            }
        ],
        "learnable": [],
        "missing": [],
        "generic": [],
        "unknown": [],
        "needs_user_input": False,
    }


def test_pending_register_dedupe_and_list(tmp_path):
    """Two alias spellings collapse into one pending entry with both vacancies."""
    init_db(tmp_path)
    for vid in ("hh-1", "hh-2"):
        created = cli(
            "pipeline_ctl",
            tmp_path,
            "--add-vacancy",
            vid,
            "--company",
            "acme",
            "--title",
            "T",
        )
        assert created.returncode == 0, created.stderr

    first = cli(
        "skills_tool", tmp_path, "--pending", "Apache Kafka", "--vacancy", "hh-1"
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {
        "ok": True,
        "added": ["Apache Kafka"],
        "already_pending": [],
        "total_pending": 1,
    }

    second = cli(
        "skills_tool",
        tmp_path,
        "--pending",
        "Kafka",
        "--vacancy",
        "hh-2",
        now="2026-02-02T00:00:00Z",
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {
        "ok": True,
        "added": [],
        "already_pending": ["Kafka"],
        "total_pending": 1,
    }

    listed = cli("skills_tool", tmp_path, "--list-pending")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {
        "pending": [
            {
                "term": "Apache Kafka",
                "key": "kafka",
                "vacancies": ["hh-1", "hh-2"],
                "first_seen": NOW,
            }
        ],
        "total": 1,
    }


def test_add_clears_pending(tmp_path):
    """--add removes the term's pending entry; a clean add reports false."""
    init_db(tmp_path)
    created = cli(
        "pipeline_ctl",
        tmp_path,
        "--add-vacancy",
        "hh-1",
        "--company",
        "acme",
        "--title",
        "T",
    )
    assert created.returncode == 0, created.stderr

    registered = cli(
        "skills_tool", tmp_path, "--pending", "Apache Kafka", "--vacancy", "hh-1"
    )
    assert registered.returncode == 0, registered.stderr

    added = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Apache Kafka",
        "--bucket",
        "confirmed",
        "--note",
        "Опыт работы с Kafka",
    )
    assert added.returncode == 0, added.stderr
    payload = json.loads(added.stdout)
    assert payload["cleared_pending"] is True
    assert payload["bucket"] == "confirmed_skills"

    listed = cli("skills_tool", tmp_path, "--list-pending")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {"pending": [], "total": 0}

    unrelated = cli(
        "skills_tool", tmp_path, "--add", "Docker", "--bucket", "learnable"
    )
    assert unrelated.returncode == 0, unrelated.stderr
    assert json.loads(unrelated.stdout)["cleared_pending"] is False


def test_list_pending_empty_db(tmp_path):
    """--list-pending on a fresh root is empty and creates no data files."""
    result = cli("skills_tool", tmp_path, "--list-pending")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"pending": [], "total": 0}
    assert not (tmp_path / "master_data").exists()


def test_generic_autoclaim(tmp_path):
    """A seeded generic competency is KNOWN without asking the user."""
    init_db(tmp_path)

    result = cli("skills_tool", tmp_path, "--check", "Аналитическое мышление")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"Аналитическое мышление": "KNOWN"},
        "ask_user": [],
    }


def test_generic_removed_by_owner(tmp_path):
    """An owner-emptied generic list sends the term back to UNKNOWN_ASK_USER."""
    init_db(tmp_path)
    conn = open_db(tmp_path)
    conn.execute("DELETE FROM skills WHERE bucket='generic'")
    conn.commit()
    conn.close()

    result = cli("skills_tool", tmp_path, "--check", "Аналитическое мышление")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"Аналитическое мышление": "UNKNOWN_ASK_USER"},
        "ask_user": ["Аналитическое мышление"],
    }


def test_alias_matches_known_terms(tmp_path):
    """Alias spellings resolve to confirmed canonical terms."""
    init_db(tmp_path)

    for term in ("excel", "ms sql"):
        added = cli(
            "skills_tool", tmp_path, "--add", term, "--bucket", "confirmed"
        )
        assert added.returncode == 0, added.stderr

    result = cli("skills_tool", tmp_path, "--check", "MS Excel,MSSQL")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"MS Excel": "KNOWN", "MSSQL": "KNOWN"},
        "ask_user": [],
    }


def test_explicit_answer_overrides_generic(tmp_path):
    """An explicit missing answer outranks the generic KNOWN auto-claim."""
    init_db(tmp_path)

    added = cli(
        "skills_tool", tmp_path, "--add", "Анализ данных", "--bucket", "missing"
    )
    assert added.returncode == 0, added.stderr

    result = cli("skills_tool", tmp_path, "--check", "Анализ данных")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "results": {"Анализ данных": "MISSING"},
        "ask_user": [],
    }


def test_vacancy_with_check_is_argparse_error(tmp_path):
    """--vacancy is only valid with --pending, so --check + --vacancy exits 2."""
    init_db(tmp_path)

    result = cli(
        "skills_tool", tmp_path, "--check", "Python", "--vacancy", "hh-1"
    )
    assert result.returncode == 2
    assert "--vacancy requires --pending" in result.stderr


def test_add_clears_alias_repointed_pending(tmp_path):
    """An alias repoint after registration still clears the stale entry."""
    init_db(tmp_path)
    created = cli(
        "pipeline_ctl",
        tmp_path,
        "--add-vacancy",
        "hh-1",
        "--company",
        "acme",
        "--title",
        "T",
    )
    assert created.returncode == 0, created.stderr

    registered = cli(
        "skills_tool", tmp_path, "--pending", "PostgreSQL", "--vacancy", "hh-1"
    )
    assert registered.returncode == 0, registered.stderr
    assert json.loads(registered.stdout)["added"] == ["PostgreSQL"]

    conn = open_db(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO skill_aliases(alias, canonical) "
        "VALUES ('postgresql', 'postgres')"
    )
    conn.commit()
    conn.close()

    added = cli(
        "skills_tool", tmp_path, "--add", "PostgreSQL", "--bucket", "confirmed"
    )
    assert added.returncode == 0, added.stderr
    payload = json.loads(added.stdout)
    assert payload["cleared_pending"] is True

    listed = cli("skills_tool", tmp_path, "--list-pending")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {"pending": [], "total": 0}

    conn = open_db(tmp_path)
    canonical = conn.execute(
        "SELECT bucket FROM skills WHERE normalized=?", ("postgres",)
    ).fetchone()
    old_key = conn.execute(
        "SELECT 1 FROM skills WHERE normalized=?", ("postgresql",)
    ).fetchone()
    conn.close()
    assert canonical is not None
    assert canonical["bucket"] == "confirmed"
    assert old_key is None


def test_audit_generic_terms_routed(tmp_path):
    """A generic competency lands in the generic group, never unknown."""
    init_db(tmp_path)

    result = cli("skills_tool", tmp_path, "--audit", "Анализ данных")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "confirmed": [],
        "rusty": [],
        "learnable": [],
        "missing": [],
        "generic": ["Анализ данных"],
        "unknown": [],
        "needs_user_input": False,
    }


def test_list_bucket_rusty_shape(tmp_path):
    """--list-bucket rusty lists only rusty entries with name/note/added_ts."""
    init_db(tmp_path)

    soap = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "SOAP",
        "--bucket",
        "rusty",
        "--note",
        "Освежить перед интервью",
    )
    assert soap.returncode == 0, soap.stderr
    xml = cli("skills_tool", tmp_path, "--add", "XML", "--bucket", "rusty")
    assert xml.returncode == 0, xml.stderr
    sql = cli("skills_tool", tmp_path, "--add", "SQL", "--bucket", "confirmed")
    assert sql.returncode == 0, sql.stderr

    result = cli("skills_tool", tmp_path, "--list-bucket", "rusty")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"bucket", "total", "skills"}
    assert payload == {
        "bucket": "rusty_skills",
        "total": 2,
        "skills": [
            {
                "name": "SOAP",
                "note": "Освежить перед интервью",
                "added_ts": NOW,
                "category": None,
            },
            {"name": "XML", "note": "", "added_ts": NOW, "category": None},
        ],
    }
    assert "SQL" not in [entry["name"] for entry in payload["skills"]]


def test_list_bucket_empty(tmp_path):
    """An empty bucket lists as an empty skill array."""
    init_db(tmp_path)

    result = cli("skills_tool", tmp_path, "--list-bucket", "missing")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "bucket": "confirmed_missing",
        "total": 0,
        "skills": [],
    }


def test_list_bucket_invalid_name_exit_2(tmp_path):
    """A bucket name outside the argparse choices is a usage error."""
    result = cli("skills_tool", tmp_path, "--list-bucket", "bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr.lower()
    assert "bogus" in result.stderr


def test_list_bucket_read_only(tmp_path):
    """--list-bucket never mutates the stored skill row."""
    init_db(tmp_path)

    added = cli("skills_tool", tmp_path, "--add", "SOAP", "--bucket", "rusty")
    assert added.returncode == 0, added.stderr

    conn = open_db(tmp_path)
    before = conn.execute(
        "SELECT note, added_ts FROM skills WHERE normalized=?", ("soap",)
    ).fetchone()
    conn.close()

    result = cli("skills_tool", tmp_path, "--list-bucket", "rusty")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["total"] == 1

    conn = open_db(tmp_path)
    after = conn.execute(
        "SELECT note, added_ts FROM skills WHERE normalized=?", ("soap",)
    ).fetchone()
    conn.close()
    assert tuple(before) == tuple(after)


def test_list_categories(tmp_path):
    """The five seeded categories list in sort_order with their ids."""
    init_db(tmp_path)

    result = cli("skills_tool", tmp_path, "--list-categories")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "categories": [
            {
                "category_id": 1,
                "name": "Системный и бизнес-анализ",
                "sort_order": 1,
            },
            {
                "category_id": 2,
                "name": "Инженерия данных и аналитика",
                "sort_order": 2,
            },
            {
                "category_id": 3,
                "name": "Data Quality, статистика и 152-ФЗ",
                "sort_order": 3,
            },
            {
                "category_id": 4,
                "name": "ИИ и мультиагентные системы",
                "sort_order": 4,
            },
            {
                "category_id": 5,
                "name": "Доменная экспертиза и инфраструктура",
                "sort_order": 5,
            },
        ],
        "total": 5,
    }


def test_list_bucket_includes_category(tmp_path):
    """--list-bucket exposes the assigned category name."""
    init_db(tmp_path)

    added = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Python",
        "--bucket",
        "confirmed",
        "--category",
        "Инженерия данных и аналитика",
    )
    assert added.returncode == 0, added.stderr
    assert (
        json.loads(added.stdout)["category_name"]
        == "Инженерия данных и аналитика"
    )

    result = cli("skills_tool", tmp_path, "--list-bucket", "confirmed")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "bucket": "confirmed_skills",
        "total": 1,
        "skills": [
            {
                "name": "Python",
                "note": "",
                "added_ts": NOW,
                "category": "Инженерия данных и аналитика",
            }
        ],
    }


def test_check_read_only_no_commit(tmp_path):
    """--check and --list-bucket leave the skill row untouched."""
    init_db(tmp_path)

    added = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Kafka",
        "--bucket",
        "confirmed",
        "--note",
        "Опыт работы с Kafka",
    )
    assert added.returncode == 0, added.stderr

    conn = open_db(tmp_path)
    before = conn.execute(
        "SELECT note, added_ts FROM skills WHERE normalized=?", ("kafka",)
    ).fetchone()
    conn.close()

    checked = cli("skills_tool", tmp_path, "--check", "Kafka")
    assert checked.returncode == 0, checked.stderr
    listed = cli("skills_tool", tmp_path, "--list-bucket", "confirmed")
    assert listed.returncode == 0, listed.stderr

    conn = open_db(tmp_path)
    after = conn.execute(
        "SELECT note, added_ts FROM skills WHERE normalized=?", ("kafka",)
    ).fetchone()
    conn.close()
    assert tuple(before) == tuple(after)
    assert after["note"] == "Опыт работы с Kafka"
    assert after["added_ts"] == NOW

def test_add_with_category(tmp_path):
    """--add --category stores the skill and reports the assigned category."""
    init_db(tmp_path)

    result = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "Python",
        "--bucket",
        "confirmed",
        "--category",
        "Инженерия данных и аналитика",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "skill": "Python",
        "bucket": "confirmed_skills",
        "category": "KNOWN",
        "category_name": "Инженерия данных и аналитика",
        "moved_from": None,
        "cleared_pending": False,
    }


def test_set_category_assigns_and_updates(tmp_path):
    """--set-category assigns a category and can repoint it later."""
    init_db(tmp_path)

    added = cli("skills_tool", tmp_path, "--add", "Python", "--bucket", "confirmed")
    assert added.returncode == 0, added.stderr

    first = cli(
        "skills_tool",
        tmp_path,
        "--set-category",
        "Python",
        "--category",
        "Инженерия данных и аналитика",
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {
        "ok": True,
        "skill": "Python",
        "category": "Инженерия данных и аналитика",
    }

    second = cli(
        "skills_tool",
        tmp_path,
        "--set-category",
        "Python",
        "--category",
        "Data Quality, статистика и 152-ФЗ",
    )
    assert second.returncode == 0, second.stderr
    assert (
        json.loads(second.stdout)["category"]
        == "Data Quality, статистика и 152-ФЗ"
    )

    listed = cli("skills_tool", tmp_path, "--list-bucket", "confirmed")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["skills"] == [
        {
            "name": "Python",
            "note": "",
            "added_ts": NOW,
            "category": "Data Quality, статистика и 152-ФЗ",
        }
    ]


def test_set_category_missing_skill_error(tmp_path):
    """--set-category fails for unknown skills and unknown categories."""
    init_db(tmp_path)

    missing = cli(
        "skills_tool",
        tmp_path,
        "--set-category",
        "Nope",
        "--category",
        "Инженерия данных и аналитика",
    )
    assert missing.returncode == 1
    assert json.loads(missing.stderr)["error"] == "skill_not_found"

    added = cli("skills_tool", tmp_path, "--add", "Python", "--bucket", "confirmed")
    assert added.returncode == 0, added.stderr

    bad_category = cli(
        "skills_tool",
        tmp_path,
        "--set-category",
        "Python",
        "--category",
        "Нет такой",
    )
    assert bad_category.returncode == 1
    assert json.loads(bad_category.stderr)["error"] == "category_not_found"


def test_add_category_creates_and_sorts(tmp_path):
    """--add-category appends in sort order and is idempotent by name."""
    init_db(tmp_path)

    created = cli("skills_tool", tmp_path, "--add-category", "Новая категория")
    assert created.returncode == 0, created.stderr
    created_payload = json.loads(created.stdout)
    assert created_payload == {
        "ok": True,
        "category_id": 6,
        "name": "Новая категория",
        "sort_order": 6,
    }

    sorted_cat = cli(
        "skills_tool", tmp_path, "--add-category", "Ещё одна", "--sort", "2"
    )
    assert sorted_cat.returncode == 0, sorted_cat.stderr
    sorted_payload = json.loads(sorted_cat.stdout)
    assert sorted_payload["sort_order"] == 2
    assert sorted_payload["name"] == "Ещё одна"

    listed = cli("skills_tool", tmp_path, "--list-categories")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["total"] == 7

    again = cli("skills_tool", tmp_path, "--add-category", "Новая категория")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["category_id"] == created_payload["category_id"]

    relisted = cli("skills_tool", tmp_path, "--list-categories")
    assert relisted.returncode == 0, relisted.stderr
    assert json.loads(relisted.stdout)["total"] == 7


def test_list_uncategorized(tmp_path):
    """--list-uncategorized excludes generic seeds and categorized skills."""
    init_db(tmp_path)

    empty = cli("skills_tool", tmp_path, "--list-uncategorized")
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == {"skills": [], "total": 0}

    python = cli("skills_tool", tmp_path, "--add", "Python", "--bucket", "confirmed")
    assert python.returncode == 0, python.stderr
    sql = cli(
        "skills_tool",
        tmp_path,
        "--add",
        "SQL",
        "--bucket",
        "confirmed",
        "--category",
        "Инженерия данных и аналитика",
    )
    assert sql.returncode == 0, sql.stderr

    result = cli("skills_tool", tmp_path, "--list-uncategorized")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "skills": [{"name": "Python", "bucket": "confirmed"}],
        "total": 1,
    }



def test_import_vault_moves_json_into_db(tmp_path):
    """--import-vault copies legacy skills, aliases and pending rows into DB."""
    init_db(tmp_path)
    created = cli(
        "pipeline_ctl",
        tmp_path,
        "--add-vacancy",
        "hh-1",
        "--company",
        "acme",
        "--title",
        "T",
    )
    assert created.returncode == 0, created.stderr

    master = tmp_path / "master_data"
    master.mkdir(parents=True, exist_ok=True)
    (master / "skills_vault.json").write_text(
        json.dumps(
            {
                "confirmed_skills": {
                    "python": {
                        "name": "Python",
                        "note": "",
                        "added_ts": None,
                    }
                },
                "aliases": {"postgres": "postgresql"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (master / "pending_skills.json").write_text(
        json.dumps(
            {
                "go": {
                    "name": "Go",
                    "vacancies": ["hh-1"],
                    "first_seen": NOW,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = cli("skills_tool", tmp_path, "--import-vault")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["skills_imported"] == 1
    assert payload["aliases_imported"] == 1
    assert payload["pending_imported"] == 1

    checked = cli("skills_tool", tmp_path, "--check", "Python")
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "results": {"Python": "KNOWN"},
        "ask_user": [],
    }

    listed = cli("skills_tool", tmp_path, "--list-pending")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {
        "pending": [
            {
                "term": "Go",
                "key": "go",
                "vacancies": ["hh-1"],
                "first_seen": NOW,
            }
        ],
        "total": 1,
    }


def test_import_vault_applies_category_map(tmp_path):
    """--map assigns a category to an imported skill by display name."""
    init_db(tmp_path)

    master = tmp_path / "master_data"
    master.mkdir(parents=True, exist_ok=True)
    (master / "skills_vault.json").write_text(
        json.dumps(
            {
                "confirmed_skills": {
                    "python": {
                        "name": "Python",
                        "note": "",
                        "added_ts": None,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    map_path = tmp_path / "map.json"
    map_path.write_text(
        json.dumps({"Инженерия данных и аналитика": ["Python"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = cli(
        "skills_tool", tmp_path, "--import-vault", "--map", str(map_path)
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["categorized"] >= 1

    listed = cli("skills_tool", tmp_path, "--list-bucket", "confirmed")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {
        "bucket": "confirmed_skills",
        "total": 1,
        "skills": [
            {
                "name": "Python",
                "note": "",
                "added_ts": None,
                "category": "Инженерия данных и аналитика",
            }
        ],
    }


def test_import_vault_idempotent(tmp_path):
    """A second --import-vault skips existing rows instead of duplicating."""
    init_db(tmp_path)

    master = tmp_path / "master_data"
    master.mkdir(parents=True, exist_ok=True)
    (master / "skills_vault.json").write_text(
        json.dumps(
            {
                "confirmed_skills": {
                    "python": {
                        "name": "Python",
                        "note": "",
                        "added_ts": None,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = cli("skills_tool", tmp_path, "--import-vault")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["skills_imported"] == 1

    second = cli("skills_tool", tmp_path, "--import-vault")
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["skills_imported"] == 0
    assert payload["skills_skipped"] == 1


def test_import_vault_respects_empty_generic(tmp_path):
    """An empty generic_competencies key replaces the seeded generic rows."""
    init_db(tmp_path)

    master = tmp_path / "master_data"
    master.mkdir(parents=True, exist_ok=True)
    (master / "skills_vault.json").write_text(
        json.dumps({"generic_competencies": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = cli("skills_tool", tmp_path, "--import-vault")
    assert result.returncode == 0, result.stderr

    checked = cli("skills_tool", tmp_path, "--check", "Аналитическое мышление")
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "results": {"Аналитическое мышление": "UNKNOWN_ASK_USER"},
        "ask_user": ["Аналитическое мышление"],
    }
