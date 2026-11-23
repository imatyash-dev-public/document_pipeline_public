"""CLI tests for scripts.docx_tool: template init, render, verify.

Every test runs the real CLI via subprocess against a fresh root under
pytest's ``tmp_path``. The payload/vacancy fixtures are synthetic Russian
data in ``tests/fixtures``; nothing reads the repo's ``master_data/``.
"""

from __future__ import annotations

import json
import sqlite3 as _sqlite3
import subprocess
import sys
from pathlib import Path

import docx
from docx.shared import RGBColor

import scripts.docx_tool as docx_tool

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAYLOAD = FIXTURES / "resume_payload.json"
TEMPLATE_REL = "master_data/resume_template.docx"


def cli(tmp_path, *args):
    """Run scripts.docx_tool with ``--root tmp_path`` and return the process."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.docx_tool",
            "--root",
            str(tmp_path),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
    )


def make_vacancy(tmp_path, vacancy_id, skills):
    """Create a vacancy row via the CLI and set its ``hard_skills``."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "scripts.pipeline_ctl",
            "--add-vacancy", vacancy_id, "--company", "acme", "--title", "T",
            "--root", str(tmp_path),
        ],
        cwd=REPO_ROOT, capture_output=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    conn = _sqlite3.connect(tmp_path / "db" / "state.sqlite")
    conn.execute(
        "UPDATE vacancies SET hard_skills=? WHERE vacancy_id=?",
        (json.dumps(skills, ensure_ascii=False), vacancy_id),
    )
    conn.commit()
    conn.close()


def test_init_template_creates_file(tmp_path):
    proc = cli(tmp_path, "--init-template")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out == {"ok": True, "template": TEMPLATE_REL}
    assert (tmp_path / TEMPLATE_REL).is_file()

    again = cli(tmp_path, "--init-template")
    assert again.returncode == 1
    err = json.loads(again.stderr)
    assert err["error"] == "template_exists"


def test_render_and_verify_pass(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    output = tmp_path / "out.docx"
    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(PAYLOAD),
        "--output",
        str(output),
    )
    assert proc.returncode == 0, proc.stderr
    rendered = json.loads(proc.stdout)
    assert rendered["ok"] is True
    assert rendered["experience_items"] == 2
    assert rendered["projects_items"] == 2
    assert rendered["skill_groups"] == 2

    make_vacancy(tmp_path, "hh-9", ["Python", "SQL", "Go"])

    verified = cli(
        tmp_path,
        "--verify",
        str(output),
        "--vacancy",
        "hh-9",
    )
    assert verified.returncode == 0, verified.stderr
    res = json.loads(verified.stdout)
    assert res["passed"] is True
    assert res["jinja_tags"] == []
    assert res["placeholders"] == []
    assert res["hidden_runs"] == 0
    assert res["ai_disclosure"] is False
    assert res["keyword_overlap_pct"] == 66.7
    assert res["matched"] == ["Python", "SQL"]
    assert res["missing"] == ["Go"]
    assert res["total_keywords"] == 3


def test_verify_detects_unrendered_tags(tmp_path):
    path = tmp_path / "unrendered.docx"
    d = docx.Document()
    d.add_paragraph("{{ summary }}")
    d.add_paragraph(docx_tool.AI_DISCLOSURE_NOTE)
    d.save(str(path))

    proc = cli(tmp_path, "--verify", str(path))
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert res["passed"] is False
    assert res["jinja_tags"]
    assert res["ai_disclosure"] is True


def test_verify_detects_placeholders_and_hidden_text(tmp_path):
    path = tmp_path / "placeholders.docx"
    d = docx.Document()
    d.add_paragraph("[TODO]")
    d.add_paragraph("Уважаемый [Имя]")

    p = d.add_paragraph()
    run = p.add_run("скрытый текст")
    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    p = d.add_paragraph()
    run = p.add_run("невидимо")
    run.font.hidden = True

    d.add_paragraph(docx_tool.AI_DISCLOSURE_NOTE)
    d.save(str(path))

    proc = cli(tmp_path, "--verify", str(path))
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert res["passed"] is False
    assert "[TODO]" in res["placeholders"]
    assert "[Имя]" in res["placeholders"]
    assert res["hidden_runs"] == 2


def test_verify_disclosure_note_fails(tmp_path):
    path = tmp_path / "disclosure.docx"
    d = docx.Document()
    d.add_paragraph(docx_tool.AI_DISCLOSURE_NOTE)
    d.save(str(path))

    proc = cli(tmp_path, "--verify", str(path))
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert res["ai_disclosure"] is True
    assert res["passed"] is False
    assert res["jinja_tags"] == []
    assert res["placeholders"] == []
    assert res["hidden_runs"] == 0


def test_verify_min_overlap_fails(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    output = tmp_path / "out.docx"
    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(PAYLOAD),
        "--output",
        str(output),
    )
    assert proc.returncode == 0, proc.stderr

    make_vacancy(tmp_path, "hh-9", ["Python", "SQL", "Go"])

    verified = cli(
        tmp_path,
        "--verify",
        str(output),
        "--vacancy",
        "hh-9",
        "--min-overlap",
        "80",
    )
    assert verified.returncode == 0, verified.stderr
    res = json.loads(verified.stdout)
    assert res["passed"] is False
    assert res["keyword_overlap_pct"] == 66.7


def test_render_missing_key_error(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    payload.pop("summary")
    bad = tmp_path / "bad_payload.json"
    bad.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(bad),
        "--output",
        str(tmp_path / "out.docx"),
    )
    assert proc.returncode == 1
    err = json.loads(proc.stderr)
    assert err["error"] == "invalid_payload"


def test_render_missing_projects_key_error(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    payload.pop("projects")
    bad = tmp_path / "bad_payload.json"
    bad.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(bad),
        "--output",
        str(tmp_path / "out.docx"),
    )
    assert proc.returncode == 1
    err = json.loads(proc.stderr)
    assert err["error"] == "invalid_payload"


def test_render_skills_group_missing_category_error(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    payload["skills"] = [{"items": ["Python"]}]
    bad = tmp_path / "bad_payload.json"
    bad.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(bad),
        "--output",
        str(tmp_path / "out.docx"),
    )
    assert proc.returncode == 1
    err = json.loads(proc.stderr)
    assert err["error"] == "invalid_payload"


def test_verify_vacancy_from_db_matches(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    output = tmp_path / "out.docx"
    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(PAYLOAD),
        "--output",
        str(output),
    )
    assert proc.returncode == 0, proc.stderr

    make_vacancy(tmp_path, "hh-9", ["Python", "SQL"])

    verified = cli(tmp_path, "--verify", str(output), "--vacancy", "hh-9")
    assert verified.returncode == 0, verified.stderr
    res = json.loads(verified.stdout)
    assert res["passed"] is True
    assert res["keyword_overlap_pct"] == 100.0
    assert res["matched"] == ["Python", "SQL"]
    assert res["missing"] == []
    assert res["total_keywords"] == 2


def test_verify_vacancy_not_found(tmp_path):
    assert cli(tmp_path, "--init-template").returncode == 0

    output = tmp_path / "out.docx"
    proc = cli(
        tmp_path,
        "--render",
        "--payload",
        str(PAYLOAD),
        "--output",
        str(output),
    )
    assert proc.returncode == 0, proc.stderr

    verified = cli(
        tmp_path, "--verify", str(output), "--vacancy", "hh-NOPE"
    )
    assert verified.returncode == 1
    err = json.loads(verified.stderr)
    assert err["error"] == "vacancy_not_found"


def test_verify_detects_stale_skill_repr(tmp_path):
    path = tmp_path / "stale.docx"
    d = docx.Document()
    d.add_paragraph("{'category': 'X', 'items': ['Y']}")
    d.save(str(path))

    proc = cli(tmp_path, "--verify", str(path))
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert res["stale_skill_repr"] is True
    assert res["passed"] is False
