# Multi-Agent ETL & Document Pipeline

A deterministic, offline engineering showcase of a **Thin-Agent /
Thick-Tool** document pipeline: LLM agents do the reasoning; a small set of
single-purpose Python CLI tools do all repeatable work (state, storage,
sanitization, rendering, QA). The repository is a public portfolio release:
**no network access, no real personal data, no proprietary scraper logic**.

## Contents & language

- Python code, identifiers, comments, CLI JSON keys, tests and docs are
  English. Synthetic sample content under `tests/fixtures/` is also
  English (or clearly synthetic Russian).
- Read/write all files as UTF-8. Never round-trip Cyrillic through
  PowerShell 5.1 pipes/redirection (its cp1251 encoding mangles it).

## Privacy & offline guarantee

- The pipeline runs on **synthetic fixtures** under
  `tests/fixtures/synthetic_sources/`; there is no real personal data in
  the tree.
- Runtime state (`db/`, `companies/`, `master_data/`) is gitignored and
  generated only by running the demo/tools. Never commit it.
- **No tool makes network calls.** The pipeline is fully offline.

## Project layout

| Path | Purpose |
|---|---|
| `scripts/common.py` | shared helpers: paths, JSON output, errors, clock |
| `scripts/db.py` | SQLite connection, schema, migrations (WAL, 12-state FSM) |
| `scripts/pipeline_ctl.py` | state & 90-day cache manager, peak gate, `--next` |
| `scripts/sanitize.py` | prompt-injection stripper (lib + CLI) |
| `scripts/ingest.py` | pluggable ingestion (`BaseIngestionAdapter` + `FixtureIngestionAdapter`) |
| `scripts/skills_tool.py` | skills vault manager + pending-question queue |
| `scripts/docx_tool.py` | resume template/render/verify (ATS QA gate) |
| `scripts/demo_tool.py` | deterministic end-to-end `--dry-run` demo |
| `requirements.txt` | pinned third-party deps |
| `tests/` | pytest suites (`test_<module>.py`) |
| `tests/fixtures/` | synthetic input data (never real data) |
| `docs/CLI.md`, `docs/DB.md` | per-tool CLI reference and data shapes |
| `README.md` | architecture, diagrams, quickstart |
| `frontend/` | reserved for a future UI; empty |

## CLI conventions (binding)

- Python >= 3.11. A third-party dependency is allowed only when pinned as
  `name==X.Y.Z` in `requirements.txt`.
- Run from the repo root: `python -m scripts.<tool> [options]`. Each tool
  uses argparse flags (no subcommands).
- Common options (on every tool that needs them): `--root PATH` (base dir
  for `db/`, `master_data/` and `companies/`, default `.`), `--db PATH`
  (default `<root>/db/state.sqlite`), `--now ISO` (UTC, default current
  time). Create parent directories when missing. Paths printed in JSON or
  stored in SQLite are relative to `--root` with forward slashes.
- Deterministic: no LLM calls, no randomness, no network. Library
  functions never read the clock; they take `now: str` (ISO 8601 UTC,
  `YYYY-MM-DDTHH:MM:SSZ`). Only each script's `main()` fills it from
  `--now` or the current UTC time.
- Success: exactly one compact JSON object on one stdout line
  (`json.dumps(obj, ensure_ascii=False, separators=(",", ":"))`), exit 0.
- Failure: `{"error": "<snake_case_code>", "message": "<text>"}` on
  stderr, exit 1. Argparse usage errors keep exit 2.
- UTF-8: call `sys.stdout.reconfigure(encoding="utf-8")` (and the same for
  stderr) at startup; open files with `encoding="utf-8"`.
- SQLite: stdlib `sqlite3`, parameterized queries only,
  `PRAGMA foreign_keys = ON` and `PRAGMA journal_mode = WAL` on every
  connection. Schema version lives in `PRAGMA user_version`; each migration
  is `migrate_<n>(conn: sqlite3.Connection) -> None` in `scripts/db.py`.

## Commands

- Tests: `python -m pytest tests -q`.
- CLI help: `python -m scripts.<tool> --help`.
- End-to-end demo: `python -m scripts.demo_tool --dry-run`.
- Per-tool reference (flags, stdout JSON, error codes, data shapes):
  `docs/CLI.md`. Behavior is pinned by the tests; the tests win on conflict.
- Install deps: `python -m pip install -r requirements.txt`.

## Thin-Agent / Thick-Tool contract

Agents never read files or write state directly. They call the CLI tools
and read/write one-line JSON. The tools are the single source of truth for
all persistent state; the agents contribute only reasoning and prose
authored into files via the tools. See `README.md` for the architecture
diagrams (12-state FSM, contract flow, FinOps routing, ingestion adapter).
