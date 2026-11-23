"""Deterministic, offline end-to-end demo driver for the document pipeline.

Walks the full 12-state SQLite FSM using only the existing deterministic
tools (ingestion fixtures, skills vault, DOCX render, ATS QA gate). There
is no network, no LLM, no randomness, and library functions never read
the clock: ``run_demo`` receives ``now``.

``python -m scripts.demo_tool --dry-run`` prints exactly one compact JSON
object.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from scripts import common, docx_tool, pipeline_ctl, skills_tool
from scripts.db import connect
from scripts.ingest import (
    FixtureIngestionAdapter,
    save_osint,
    search_and_persist,
)


def run_demo(
    root: Path,
    db_path: Path,
    now: str,
    source_dir: Path,
    min_overlap: float,
) -> dict:
    """Drive the deterministic pipeline end-to-end and return its summary."""
    root = Path(root)
    db_path = Path(db_path)
    source_dir = Path(source_dir)
    states_visited: list[str] = []

    def record(status: str) -> None:
        if status not in states_visited:
            states_visited.append(status)

    conn = connect(db_path)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        schema = conn.execute("PRAGMA user_version").fetchone()[0]

        def set_status(
            vacancy_id: str,
            status: str,
            score: int | None = None,
        ) -> None:
            pipeline_ctl.update_vacancy(
                conn, vacancy_id, status, now, score
            )
            record(status)

        docx_tool.build_template(docx_tool.default_template_path(root))

        gate = pipeline_ctl.check_peak_gate(common.parse_ts(now))

        adapter = FixtureIngestionAdapter(source_dir)
        search_and_persist(
            conn, root, adapter, "engineer", "113", 20, 0, now
        )
        record("DISCOVERED")

        set_status("1002", "SKIPPED_LOW_MATCH", score=45)

        save_osint(
            conn,
            root,
            "acme-analytics",
            source_dir / "dossier" / "acme_dossier.md",
            now,
        )
        set_status("1001", "OSINT_READY")
        skills_tool.add_pending(conn, ["Kubernetes"], "1001", now)
        set_status("1001", "AWAITING_SKILL_CLARIFICATION")
        skills_tool.save_skill(conn, "Kubernetes", "learnable", "", now)
        set_status("1001", "PLAN_READY")

        payload = source_dir / "resume_payload_synthetic.json"
        out = (
            root
            / "companies"
            / "acme-analytics"
            / "vacancies"
            / "1001"
            / "resume.docx"
        )
        docx_tool.render(
            docx_tool.default_template_path(root), payload, out
        )
        set_status("1001", "DRAFT_READY")

        qa = docx_tool.verify(out, "1001", min_overlap, db_path)

        set_status("1001", "QA_PASSED")
        pipeline_ctl.set_wave(conn, "1001", 1, now)
        set_status("1001", "DISPATCHED")
        pipeline_ctl.set_result(conn, "1001", "Synthetic outcome", now)
        set_status("1001", "CLOSED")

        save_osint(
            conn,
            root,
            "cloudgrid",
            source_dir / "dossier" / "acme_dossier.md",
            now,
        )
        set_status("1003", "OSINT_READY")
        set_status("1003", "PLAN_READY")
        set_status("1003", "DRAFT_READY")
        set_status("1003", "QA_PASSED")

        description = pipeline_ctl.get_vacancy(conn, "1003")["description"]
        adversarial_redacted = "[REDACTED_INJECTION_ATTEMPT]" in description

        pipeline_ctl.upsert_vacancy(
            conn,
            "flaky-1",
            "acme-analytics",
            "Flaky Vacancy",
            None,
            "companies/acme-analytics/vacancies/flaky-1",
            now,
        )
        record("DISCOVERED")
        set_status("flaky-1", "DRAFT_READY")
        set_status("flaky-1", "QA_FAILED_STRATEGY")
        set_status("flaky-1", "QA_FAILED_WRITER")
        pipeline_ctl.halt_exhausted(conn, now)
        record("HALTED_MAX_RETRIES")

        vacancies = [
            {"vacancy_id": row["vacancy_id"], "status": row["status"]}
            for row in conn.execute(
                "SELECT vacancy_id, status FROM vacancies "
                "ORDER BY vacancy_id"
            ).fetchall()
        ]
    finally:
        conn.close()

    return {
        "ok": True,
        "gate": gate,
        "schema_version": schema,
        "journal_mode": journal,
        "states_visited": states_visited,
        "vacancies": vacancies,
        "qa_gate": qa,
        "qa_gate_passed": bool(qa.get("passed")),
        "adversarial_redacted": adversarial_redacted,
        "docx": common.rel_path(out, root),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the demo CLI."""
    parser = argparse.ArgumentParser(prog="demo_tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--mock-llm", action="store_true")

    parser.add_argument(
        "--source-dir",
        default="tests/fixtures/synthetic_sources",
    )
    parser.add_argument(
        "--min-overlap",
        type=float,
        default=85.0,
        dest="min_overlap",
    )

    common.add_common_args(parser, db=True)
    return parser


def handle(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> dict:
    """Resolve paths and run the deterministic demo."""
    root = common.resolve_root(args)
    db_path = common.resolve_db(args)
    now = common.resolve_now(args)
    return run_demo(
        root,
        db_path,
        now,
        Path(args.source_dir),
        args.min_overlap,
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the CLI handler, returning the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return common.run(lambda: handle(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
