"""Resume DOCX template and render CLI for the document pipeline.

``python -m scripts.docx_tool`` prints exactly one compact JSON object.
Uses python-docx for template building and docxtpl for rendering.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator
from pathlib import Path

import docx
from docx.shared import Pt
from docx.text.paragraph import Paragraph
from docxtpl import DocxTemplate

from scripts import common
from scripts.db import connect


AI_DISCLOSURE_NOTE = (
    "Это резюме адаптировано под вакансию моей собственной мультиагентной "
    "системой из 5 агентов на Kilo Code (OSINT-анализ компании и адаптация "
    "резюме)."
)

REQUIRED_KEYS = (
    "full_name",
    "summary",
    "experience",
    "ai_bullets",
    "projects",
    "skills",
    "education",
)

JINJA_RE = re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}")
PLACEHOLDER_RE = re.compile(r"\[(?:TODO|TBD|FIXME|XXX|Имя|Фамилия|ФИО|Компания|Должность|Город|Телефон|Email|E-mail|Дата|Вставить[^\]]*|Укажите[^\]]*|Insert[^\]]*|Placeholder[^\]]*)\]", re.IGNORECASE)


def default_template_path(root: Path) -> Path:
    """Return the default resume template path under ``root``."""
    return root / "master_data" / "resume_template.docx"


def build_template(path: Path) -> None:
    """Build the resume DOCX template with docxtpl Jinja placeholders."""
    doc = docx.Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    doc.add_heading("{{ full_name }}", level=0)
    doc.add_paragraph("{{ headline }}")
    doc.add_paragraph("{{ contacts }}")

    doc.add_heading("Профиль", level=1)
    doc.add_paragraph("{{ summary }}")

    doc.add_heading("Опыт работы", level=1)
    doc.add_paragraph("{%p for job in experience %}")
    p = doc.add_paragraph("{{ job.position }} — {{ job.company }}")
    p.runs[0].bold = True
    p = doc.add_paragraph("{{ job.period }}")
    p.runs[0].italic = True
    doc.add_paragraph("{%p for b in job.bullets %}")
    doc.add_paragraph("{{ b }}", style="List Bullet")
    doc.add_paragraph("{%p endfor %}")
    doc.add_paragraph("{%p endfor %}")

    doc.add_heading("ИИ и инженерные проекты", level=1)
    doc.add_paragraph("{%p for proj in projects %}")
    p = doc.add_paragraph("{{ proj.name }}")
    p.runs[0].bold = True
    doc.add_paragraph("{%p if proj.role %}")
    doc.add_paragraph("Роль: {{ proj.role }}")
    doc.add_paragraph("{%p endif %}")
    doc.add_paragraph("{%p if proj.stack %}")
    doc.add_paragraph("Стек: {{ proj.stack }}")
    doc.add_paragraph("{%p endif %}")
    doc.add_paragraph("{%p for b in proj.bullets %}")
    doc.add_paragraph("{{ b }}", style="List Bullet")
    doc.add_paragraph("{%p endfor %}")
    doc.add_paragraph("{%p endfor %}")

    doc.add_heading("AI и агентные системы", level=1)
    doc.add_paragraph("{%p for b in ai_bullets %}")
    doc.add_paragraph("{{ b }}", style="List Bullet")
    doc.add_paragraph("{%p endfor %}")

    doc.add_heading("Навыки", level=1)
    doc.add_paragraph("{%p for g in skills %}")
    p = doc.add_paragraph("{{ g.category }}")
    p.runs[0].bold = True
    doc.add_paragraph("{{ g['items'] | join(', ') }}")
    doc.add_paragraph("{%p endfor %}")

    doc.add_heading("Образование", level=1)
    doc.add_paragraph("{%p for e in education %}")
    doc.add_paragraph(
        "{{ e.degree }}, {{ e.institution }}, {{ e.period }}"
    )
    doc.add_paragraph("{%p endfor %}")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def init_template(root: Path, force: bool) -> dict:
    """Create the default resume template unless it exists and ``force`` is off."""
    path = default_template_path(root)
    if path.exists() and not force:
        raise common.CliError("template_exists", "use --force to overwrite")
    build_template(path)
    return {"ok": True, "template": common.rel_path(path, root)}


def validate_payload(p: object) -> dict:
    """Validate a render payload and default the optional headline/contacts."""
    if not isinstance(p, dict):
        raise common.CliError("invalid_payload", "payload must be an object")
    for key in REQUIRED_KEYS:
        if key not in p:
            raise common.CliError("invalid_payload", f"missing key: {key}")
    for key in ("experience", "ai_bullets", "projects", "skills", "education"):
        if not isinstance(p[key], list):
            raise common.CliError(
                "invalid_payload", f"{key} must be a list"
            )
    for i, item in enumerate(p["experience"]):
        if not isinstance(item, dict):
            raise common.CliError("invalid_payload", f"experience[{i}]")
        for key in ("company", "position", "period", "bullets"):
            if key not in item:
                raise common.CliError(
                    "invalid_payload", f"experience[{i}].{key}"
                )
        if not isinstance(item["bullets"], list):
            raise common.CliError(
                "invalid_payload", f"experience[{i}].bullets"
            )
    for i, item in enumerate(p["projects"]):
        if not isinstance(item, dict):
            raise common.CliError("invalid_payload", f"projects[{i}]")
        if not isinstance(item.get("name"), str):
            raise common.CliError("invalid_payload", f"projects[{i}].name")
        if not isinstance(item.get("bullets"), list):
            raise common.CliError("invalid_payload", f"projects[{i}].bullets")
        item.setdefault("role", "")
        item.setdefault("stack", "")
    for i, item in enumerate(p["skills"]):
        if not isinstance(item, dict):
            raise common.CliError("invalid_payload", f"skills[{i}]")
        if not isinstance(item.get("category"), str):
            raise common.CliError("invalid_payload", f"skills[{i}].category")
        if not isinstance(item.get("items"), list):
            raise common.CliError("invalid_payload", f"skills[{i}].items")
        if not all(isinstance(s, str) for s in item["items"]):
            raise common.CliError("invalid_payload", f"skills[{i}].items")
    for i, item in enumerate(p["education"]):
        if not isinstance(item, dict):
            raise common.CliError("invalid_payload", f"education[{i}]")
        for key in ("degree", "institution", "period"):
            if key not in item:
                raise common.CliError(
                    "invalid_payload", f"education[{i}].{key}"
                )

    ctx = dict(p)
    ctx["headline"] = ctx.get("headline", "")
    ctx["contacts"] = ctx.get("contacts", "")
    return ctx


def render(template: Path, payload_path: Path, output: Path) -> dict:
    """Render ``payload_path`` into ``output`` using the DOCX template."""
    if not template.is_file():
        raise common.CliError(
            "template_not_found",
            "run: python -m scripts.docx_tool --init-template",
        )
    ctx = validate_payload(common.read_json(payload_path))
    tpl = DocxTemplate(str(template))
    tpl.render(ctx, autoescape=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    tpl.save(str(output))
    return {
        "ok": True,
        "output": str(output),
        "experience_items": len(ctx["experience"]),
        "projects_items": len(ctx["projects"]),
        "skill_groups": len(ctx["skills"]),
    }


def iter_paragraphs(doc) -> Iterator[Paragraph]:
    """Yield body, table-cell, then header/footer paragraphs of ``doc``."""
    for p in doc.paragraphs:
        yield p
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p
    for section in doc.sections:
        for p in section.header.paragraphs:
            yield p
        for p in section.footer.paragraphs:
            yield p


def run_is_hidden(run) -> bool:
    """Return True when a run is hidden from readers."""
    if run.font.hidden is True:
        return True
    if run.font.size is not None and run.font.size.pt < 2:
        return True
    try:
        if (
            run.font.color is not None
            and run.font.color.type is not None
            and str(run.font.color.rgb).upper() == "FFFFFF"
        ):
            return True
    except (AttributeError, ValueError):
        pass
    return False


def keyword_overlap(text: str, skills: list[str]) -> dict:
    """Measure whole-term skill overlap between ``text`` and ``skills``."""
    norm = common.normalize_term(text)
    seen: set[str] = set()
    unique: list[str] = []
    for skill in skills:
        key = common.normalize_term(skill)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(skill)
    matched: list[str] = []
    missing: list[str] = []
    for skill in unique:
        term = common.normalize_term(skill)
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        if re.search(pattern, norm):
            matched.append(skill)
        else:
            missing.append(skill)
    total = len(unique)
    return {
        "keyword_overlap_pct": round(100 * len(matched) / total, 1)
        if total
        else None,
        "matched": matched,
        "missing": missing,
        "total_keywords": total,
    }


def verify(
    docx_path: Path, vacancy_id: str | None, min_overlap: float, db_path: Path
) -> dict:
    """Run the deterministic ATS/QA check on a rendered DOCX file."""
    if not docx_path.is_file():
        raise common.CliError("file_not_found", str(docx_path))
    try:
        doc = docx.Document(str(docx_path))
    except Exception as e:
        raise common.CliError("docx_unreadable", str(e))

    jinja_tags: list[str] = []
    placeholders: list[str] = []
    hidden_runs = 0
    texts: list[str] = []
    for p in iter_paragraphs(doc):
        text = p.text
        texts.append(text)
        if JINJA_RE.search(text):
            jinja_tags.append(text[:80])
        for m in PLACEHOLDER_RE.finditer(text):
            placeholders.append(m.group(0))
        for run in p.runs:
            if run.text.strip() and run_is_hidden(run):
                hidden_runs += 1

    full_text = "\n".join(texts)
    ai_disclosure = AI_DISCLOSURE_NOTE in full_text
    stale_skill_repr = "{'category':" in full_text

    skills: list[str] = []
    if vacancy_id is not None:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT hard_skills FROM vacancies WHERE vacancy_id=?",
                (vacancy_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise common.CliError("vacancy_not_found", vacancy_id)
        raw = row["hard_skills"]
        if raw is not None:
            skills = json.loads(raw)
            if not isinstance(skills, list):
                raise common.CliError(
                    "invalid_vacancy", "hard_skills must be a list"
                )

    ov = keyword_overlap(full_text, skills)
    overlap_ok = (
        ov["keyword_overlap_pct"] is None
        or ov["keyword_overlap_pct"] >= min_overlap
    )
    passed = (
        not jinja_tags
        and not placeholders
        and hidden_runs == 0
        and not ai_disclosure
        and not stale_skill_repr
        and overlap_ok
    )
    return {
        "passed": passed,
        "jinja_tags": jinja_tags,
        "placeholders": placeholders,
        "hidden_runs": hidden_runs,
        "ai_disclosure": ai_disclosure,
        "stale_skill_repr": stale_skill_repr,
        "keyword_overlap_pct": ov["keyword_overlap_pct"],
        "matched": ov["matched"],
        "missing": ov["missing"],
        "total_keywords": ov["total_keywords"],
        "min_overlap": min_overlap,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the DOCX template/render CLI."""
    parser = argparse.ArgumentParser(prog="docx_tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init-template", action="store_true")
    group.add_argument("--render", action="store_true")
    group.add_argument("--verify", metavar="DOCX")

    parser.add_argument("--payload")
    parser.add_argument("--output")
    parser.add_argument("--vacancy")
    parser.add_argument("--template", default=None)
    parser.add_argument("--min-overlap", type=float, default=0.0,
                        dest="min_overlap")
    parser.add_argument("--force", action="store_true")

    common.add_common_args(parser, db=True)
    return parser


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict:
    """Resolve paths and dispatch the requested action."""
    root = common.resolve_root(args)

    if args.init_template:
        return init_template(root, args.force)

    if args.render:
        if not args.payload or not args.output:
            parser.error("--render requires --payload and --output")
        template = (
            Path(args.template)
            if args.template
            else default_template_path(root)
        )
        output = Path(args.output)
        result = render(template, Path(args.payload), output)
        return {
            "ok": True,
            "output": common.rel_path(output, root),
            "experience_items": result["experience_items"],
            "projects_items": result["projects_items"],
            "skill_groups": result["skill_groups"],
        }

    if args.verify:
        db_path = common.resolve_db(args)
        return verify(Path(args.verify), args.vacancy, args.min_overlap, db_path)

    raise common.CliError("not_implemented", "action")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the CLI handler, returning the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return common.run(lambda: handle(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
