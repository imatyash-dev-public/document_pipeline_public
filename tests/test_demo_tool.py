"""End-to-end tests for the deterministic offline demo driver."""

from __future__ import annotations

from pathlib import Path

import scripts.demo_tool as demo

SOURCE_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic_sources"

ALL_STATES = {
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
}


def run(tmp_path, min_overlap=85.0):
    """Run the demo against a fresh DB and root under tmp_path."""
    return demo.run_demo(
        tmp_path,
        tmp_path / "db" / "state.sqlite",
        "2026-01-01T00:00:00Z",
        SOURCE_DIR,
        min_overlap,
    )


def test_dry_run_walks_all_12_states(tmp_path) -> None:
    """The dry run visits every one of the 12 FSM states across four vacancies."""
    d = run(tmp_path)
    assert d["ok"] is True
    assert set(d["states_visited"]) == ALL_STATES
    assert d["schema_version"] == 4
    assert d["journal_mode"] == "wal"
    ids = [v["vacancy_id"] for v in d["vacancies"]]
    assert ids == sorted(ids)
    assert ids == ["1001", "1002", "1003", "flaky-1"]


def test_dry_run_docx_passes_85_gate(tmp_path) -> None:
    """The happy-path .docx clears the 85% keyword-overlap QA gate."""
    d = run(tmp_path)
    assert d["qa_gate_passed"] is True
    assert d["qa_gate"]["keyword_overlap_pct"] >= 85
    assert (tmp_path / d["docx"]).is_file()


def test_dry_run_adversarial_redacted(tmp_path) -> None:
    """The adversarial fixture injection is redacted in stored state."""
    d = run(tmp_path)
    assert d["adversarial_redacted"] is True


def test_dry_run_loop_breaker_halts_after_two(tmp_path) -> None:
    """The flaky vacancy stops at HALTED_MAX_RETRIES after two failures."""
    d = run(tmp_path)
    assert "HALTED_MAX_RETRIES" in d["states_visited"]
    flaky = [v for v in d["vacancies"] if v["vacancy_id"] == "flaky-1"]
    assert len(flaky) == 1
    assert flaky[0]["status"] == "HALTED_MAX_RETRIES"


def test_dry_run_no_network_import(tmp_path) -> None:
    """The demo module imports no network fetch helper and runs offline."""
    assert hasattr(demo, "fetch") is False
    d = run(tmp_path)
    assert d["ok"] is True
