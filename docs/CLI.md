# CLI reference — document pipeline toolbelt

Reference for the deterministic CLI tools in `scripts/`. Written for LLM
agents that must **call** these tools without reading their source.

- Conventions (JSON contract, `--root`/`--db`/`--now`, privacy, offline
  guarantee): see `AGENTS.md` → "CLI conventions". Do not duplicate them here.
- Behavior is pinned by `python -m pytest tests -q`. If this file and the
  tests disagree, **the tests win** — fix this file.
- Run everything from the repo root: `python -m scripts.<tool> ...`.
- **The public release is fully offline**: no tool makes network calls.

## Shared contract (recap)

- Success: one compact JSON object on one stdout line, exit `0`.
- Failure: `{"error":"<snake_case_code>","message":"<text>"}` on stderr,
  exit `1`. Argparse usage errors exit `2`.
- `--root PATH` (default `.`): base dir for `db/`, `master_data/`,
  `companies/`. `--db PATH` (default `<root>/db/state.sqlite`).
  `--now ISO` (`YYYY-MM-DDTHH:MM:SSZ`, UTC; default = current time).
  Where a tool actually uses the clock, a malformed `--now` is an error:
  `invalid_timestamp` (exit 1). `docx_tool` accepts `--now` but ignores it.
- Paths in JSON output and in SQLite are relative to `--root`, with
  forward slashes.
- Library functions never read the clock; only `main()` fills `now`.

## Tools at a glance

| Tool | Purpose | Touches DB |
|---|---|---|
| `pipeline_ctl` | state, URL/dossier cache, peak gate, `--next` | yes |
| `sanitize` | strip HTML/hidden text + injection redaction | no |
| `ingest` | pluggable ingestion (fixture adapter) | yes |
| `skills_tool` | skills vault check/add/list-bucket + pending queue | yes |
| `docx_tool` | resume template, render, verify | no (verify reads) |
| `demo_tool` | deterministic end-to-end `--dry-run` demo | yes |

---

## `pipeline_ctl`

Pipeline state and 90-day cache manager. Exactly one action flag is
required.

```
python -m scripts.pipeline_ctl (--init | --gate-check | --check-url U |
    --mark-url U | --check-company SLUG | --add-vacancy ID |
    --update-vacancy ID | --get-vacancy ID | --set-wave ID | --set-result ID |
    --backfill-details | --next | --summary) [--status S] [--score N]
    [--wave N] [--result TEXT] [--status-code N] [--company SLUG] [--title T]
    [--url U] [--root R] [--now ISO] [--db P]
```

| Action | Extra flags | What it does |
|---|---|---|
| `--init` | — | Create/migrate DB tables (12-status FSM, WAL) + seed the 5 skill categories |
| `--gate-check` | — | FinOps peak-cost gate: `GATE_OK` or `GATE_BLOCKED` (+`window`, `opens_at`); weekdays only; never touches SQLite |
| `--check-url U` | — | `CACHE_SKIP` if scraped < 90 days ago, else `ALLOW_SCRAPE` |
| `--mark-url U` | `--status-code N` | Record/replace a URL-cache entry stamped `--now` |
| `--check-company SLUG` | — | `CACHE_HIT` if a fresh dossier exists, else `NEEDS_OSINT` |
| `--add-vacancy ID` | `--company SLUG` (req), `--title T` (req), `--url U` | Insert vacancy as `DISCOVERED`; auto-creates the company |
| `--update-vacancy ID` | `--status S` (req), `--score N` | Set status; `--score` sets `match_score` |
| `--get-vacancy ID` | — | Full structured record for one vacancy (see shape below) |
| `--set-wave ID` | `--wave N` (req, 1..3) | Record the dispatch wave assignment |
| `--set-result ID` | `--result TEXT` (req) | Record the free-form outcome |
| `--backfill-details` | — | One-off: copy legacy `companies/*/vacancies/*/1_vacancy_clean.json` fields into the DB |
| `--next` | — | The one next action for the manager, across active items |
| `--summary` | — | Compact batch table |

Missing `--company`/`--title` (add) or `--status` (update) → exit `2`.

Output (one JSON line each):

```jsonc
// --init
{"ok":true,"db":"db/state.sqlite","schema_version":4,"categories":5}
// --gate-check
{"result":"GATE_OK","window":null,"opens_at":null}
{"result":"GATE_BLOCKED","window":"05:45-10:00Z","opens_at":"2026-11-16T10:00:00Z"}
// --check-url
{"result":"ALLOW_SCRAPE","last_scraped_ts":null,"age_days":null}
{"result":"CACHE_SKIP","last_scraped_ts":"2026-01-01T00:00:00Z","age_days":10.0}
// --check-company
{"result":"CACHE_HIT","dossier_path":"companies/acme/exec_dossier.md","age_days":30.0}
{"result":"NEEDS_OSINT","reason":"no_company"}   // or "no_dossier" | "expired"
// --update-vacancy
{"ok":true,"vacancy_id":"1001","status":"QA_FAILED_WRITER","qa_attempts":1,"match_score":80}
// --next
{"action":"SCORE_MATCH","vacancy_id":"1001","company_slug":"acme-analytics","status":"DISCOVERED","qa_attempts":0,"match_score":null,"folder_path":"companies/acme-analytics/vacancies/1001","remaining":1,"halted":[]}
{"action":"IDLE","remaining":0,"halted":[]}
```

`--gate-check` details (call it before starting any billable batch work):

- Blocked UTC windows, **weekdays only** (Mon–Fri): `00:45 <= HH:MM < 04:00`
  and `05:45 <= HH:MM < 10:00`. Sat/Sun are always `GATE_OK`.
- `GATE_BLOCKED` carries `window` and `opens_at`; both are `null` on
  `GATE_OK`. Exit code is `0` for both results.
- Pure computation: no SQLite, no files.

`--next` details:

- Before choosing, it **halts** any vacancy in `QA_FAILED_*` with
  `qa_attempts >= 2`: status → `HALTED_MAX_RETRIES` (ids in `halted`).
- Picks the single highest-priority active item (failed QA first, then
  `DRAFT_READY`, `PLAN_READY`, `OSINT_READY`, `DISCOVERED`, then
  `AWAITING_SKILL_CLARIFICATION`); ties break by `created_ts`, then id.
- `remaining` = number of active (non-terminal) vacancies.

Errors: `invalid_status`, `invalid_score`, `invalid_wave`,
`vacancy_not_found`, `invalid_slug`, `invalid_vacancy_id`,
`invalid_timestamp`.

---

## `sanitize`

Strips HTML, hidden elements and invisible Unicode, and redacts common
prompt-injection phrases (EN + RU). Run it on raw text **before any LLM
sees it**.

```
python -m scripts.sanitize [--input-file P] [--output-file P]
```

No `--root`/`--now`/`--db`. Reads `--input-file` (UTF-8) or stdin.
Without `--output-file` the cleaned `text` is in the JSON; with it the
text is written to the file and omitted from JSON. Redaction warnings go to
**stderr**.

```jsonc
{"text":"...","removed_invisible":0,"injections":["ru_forget_previous"],"chars_in":120,"chars_out":40}
{"ok":true,"output_file":"out/clean.txt","removed_invisible":0,"injections":[],"chars_in":120,"chars_out":40}
```

`injections` lists the matched pattern ids; redacted text is replaced with
`[REDACTED_INJECTION_ATTEMPT]`. Errors: `file_not_found`.

Library API: `sanitize_text(raw: str) -> dict`.

---

## `ingest`

Pluggable, offline ingestion. The public release ships the
`FixtureIngestionAdapter`, which reads synthetic sources from a directory;
payloads pass through the same `sanitize_text` pipeline as production.

```
python -m scripts.ingest (--search QUERY | --market-scan QUERY | --save-osint SLUG)
    [--source-dir DIR] [--area ID] [--per-page N] [--page N]
    [--company NAME] [--file P] [--root R] [--now ISO] [--db P]
```

| Mode | Effect |
|---|---|
| `--search QUERY` | Read `search/<slug>.json`, fetch each vacancy detail, sanitize and persist every new record as `DISCOVERED` |
| `--market-scan QUERY` | Read-only aggregate stats over `market/market_<slug>.json` items; no DB, no files |
| `--save-osint SLUG` | Copy a dossier file to `companies/<slug>/exec_dossier.md` and stamp `last_osint_ts` (`--file` required) |

```jsonc
// --search
{"ok":true,"query":"engineer","found":3,"saved":[{"vacancy_id":"1001","company_slug":"acme-analytics","folder_path":"companies/acme-analytics/vacancies/1001"}],"skipped_cached":[],"errors":[]}
// --market-scan
{"ok":true,"query":"engineer","area":"113","found":77,"scanned":5,"with_salary":4,"without_salary":1,"currencies":{"RUR":3,"USD":1},"salary":{"median":150.0,"p25":135.0,"p75":155.0,"min":120.0,"max":160.0,"currency":"RUR","sample":3},"sample_titles":["Data Engineer","…"],"injections":0}
// --save-osint
{"ok":true,"company_slug":"acme","dossier_path":"companies/acme/exec_dossier.md","chars":512,"last_osint_ts":"2026-01-01T00:00:00Z","injections":[]}
```

Errors: `source_dir_not_found`, `fixture_not_found`, `file_not_found`,
`empty_dossier`, `invalid_slug`, `invalid_timestamp`.

---

## `skills_tool`

Keeps the skills vault (claim buckets) + skill categories (resume classes)
so the Expert agent does not re-ask about skills. DB-backed.

```
python -m scripts.skills_tool (--check "A,B" | --add NAME | --audit "A,B"
    | --pending "A,B" --vacancy ID | --list-pending | --list-bucket NAME
    | --list-categories | --add-category NAME | --set-category SKILL
    | --list-uncategorized | --import-vault)
    [--bucket {confirmed,rusty,learnable,missing}] [--note TEXT]
    [--category NAME] [--sort N] [--map FILE] [--root R] [--db P] [--now ISO]
```

Skills, aliases and the pending queue live in SQLite (`skills`,
`skill_aliases`, `pending_skills`, `pending_skill_vacancies`). Run
`pipeline_ctl --init` first (migrates DB, seeds the 5 categories).
`--add` requires `--bucket` (else exit `2`). `--pending` and `--vacancy`
require each other. `--set-category` requires `--category`.

Claim buckets: `confirmed` / `rusty` / `learnable` / `missing` / `generic`.
Lookup precedence: alias resolution → `confirmed` → `rusty` → `learnable` →
`missing` → `generic` (→ `KNOWN`) → `UNKNOWN_ASK_USER`.

```jsonc
// --check
{"results":{"Python":"KNOWN","SOAP":"RUSTY_NEEDS_REFRESH","Go":"UNKNOWN_ASK_USER"},"ask_user":["Go"]}
// --add (also clears the term's pending-queue entry)
{"ok":true,"skill":"Go","bucket":"learnable_80h_whitelist","category":"LEARNABLE_80H","category_name":null,"moved_from":null,"cleared_pending":false}
// --pending "Apache Kafka" --vacancy 1001
{"ok":true,"added":["Apache Kafka"],"already_pending":[],"total_pending":1}
// --list-pending
{"pending":[{"term":"Apache Kafka","key":"kafka","vacancies":["1001"],"first_seen":"2026-01-01T00:00:00Z"}],"total":1}
```

Claim categories: `KNOWN`, `RUSTY_NEEDS_REFRESH`, `LEARNABLE_80H`,
`MISSING`, `UNKNOWN_ASK_USER`.

---

## `docx_tool`

Build the resume template, render a Writer payload, and run the
deterministic ATS/QA check.

```
python -m scripts.docx_tool (--init-template | --render | --verify DOCX)
    [--payload P] [--output P] [--vacancy ID] [--template P]
    [--min-overlap N] [--force] [--root R] [--db P] [--now ISO]
```

Default template: `<root>/master_data/resume_template.docx`; default DB:
`<root>/db/state.sqlite`. `--render` requires `--payload` and `--output`.
`--verify --vacancy ID` reads the vacancy's `hard_skills` from the DB.

| Action | Output |
|---|---|
| `--init-template` | `{"ok":true,"template":"master_data/resume_template.docx"}` |
| `--render --payload P --output P` | `{"ok":true,"output":"companies/acme-analytics/vacancies/1001/resume.docx","experience_items":2,"projects_items":2,"skill_groups":5}` |
| `--verify DOCX [--vacancy ID] [--min-overlap N]` | the check dict below |

`--verify` output (exit `0` even when `passed` is `false`):

```jsonc
{"passed":false,"jinja_tags":["{{ summary }}"],"placeholders":["[TODO]"],"hidden_runs":1,"ai_disclosure":false,"stale_skill_repr":false,"keyword_overlap_pct":66.7,"matched":["Python","SQL"],"missing":["Go"],"total_keywords":3,"min_overlap":0.0}
```

- `keyword_overlap_pct`: whole-term overlap of the vacancy's `hard_skills`
  against the document text; `--min-overlap` makes `passed` require that
  percentage (the QA gate uses `>= 85`).
- `jinja_tags` / `placeholders` / `hidden_runs` / `ai_disclosure` /
  `stale_skill_repr` flag template leftovers and hidden/white/invisible
  runs; any of them makes `passed` false.

Errors: `template_not_found`, `template_exists`, `invalid_payload`,
`file_not_found`, `invalid_json`, `docx_unreadable`, `invalid_vacancy`,
`vacancy_not_found`.

---

## `demo_tool`

Deterministic end-to-end `--dry-run` (alias `--mock-llm`). Ingests synthetic
fixtures, walks every one of the 12 FSM states, renders a `.docx`, and
verifies it passes the `>= 85%` ATS gate — no LLM, no network, no
randomness.

```
python -m scripts.demo_tool (--dry-run | --mock-llm)
    [--source-dir DIR] [--min-overlap N] [--root R] [--db P] [--now ISO]
```

```jsonc
{"ok":true,"gate":{"result":"GATE_OK","window":null,"opens_at":null},"schema_version":4,"journal_mode":"wal","states_visited":["DISCOVERED","SKIPPED_LOW_MATCH","OSINT_READY","AWAITING_SKILL_CLARIFICATION","PLAN_READY","DRAFT_READY","QA_PASSED","DISPATCHED","CLOSED","QA_FAILED_STRATEGY","QA_FAILED_WRITER","HALTED_MAX_RETRIES"],"vacancies":[{"vacancy_id":"1001","status":"CLOSED"}],"qa_gate":{...},"qa_gate_passed":true,"adversarial_redacted":true,"docx":"companies/acme-analytics/vacancies/1001/resume.docx"}
```

---

## Data shapes & lifecycle

### Vacancy statuses (DB `CHECK` constraint, 12 states)

`DISCOVERED`, `SKIPPED_LOW_MATCH`, `OSINT_READY`,
`AWAITING_SKILL_CLARIFICATION`, `PLAN_READY`, `DRAFT_READY`,
`QA_FAILED_WRITER`, `QA_FAILED_STRATEGY`, `QA_PASSED`, `DISPATCHED`,
`HALTED_MAX_RETRIES`, `CLOSED`.

Terminal (excluded from `--next`): `SKIPPED_LOW_MATCH`, `QA_PASSED`,
`HALTED_MAX_RETRIES`, `DISPATCHED`, `CLOSED`.

### `--next` action map

| Vacancy status | `--next` returns |
|---|---|
| `DISCOVERED` (no score) | `SCORE_MATCH` |
| `DISCOVERED` (scored) | `RUN_OSINT` or `REUSE_OSINT` (+`dossier_path`) |
| `OSINT_READY` | `BUILD_PLAN` |
| `AWAITING_SKILL_CLARIFICATION` | `ASK_USER_SKILLS` |
| `PLAN_READY` | `WRITE_DRAFT` |
| `DRAFT_READY` | `RUN_QA` |
| `QA_FAILED_WRITER` | `REWRITE_DRAFT` |
| `QA_FAILED_STRATEGY` | `REBUILD_PLAN` |
| `QA_FAILED_*` with `qa_attempts >= 2` | halts → `HALTED_MAX_RETRIES` |
| nothing active | `IDLE` |

These transitions are **agent-driven and not enforced**: the DB only
constrains which status *values* are allowed, and the CLI only automates
the `HALTED_MAX_RETRIES` step.

### 90-day rule

A cached URL or dossier is **fresh** iff `age_days < 90`; exactly 90.0 days
is expired.

### SQLite (`db/state.sqlite`, `user_version = 4`, WAL)

- `companies(company_slug PK, company_name, domain, dossier_path, last_osint_ts)`
- `vacancies(vacancy_id PK, company_slug FK→companies, title, url,
  match_score, status, qa_attempts, folder_path, created_ts, updated_ts,
  wave, result, area, experience, salary, hard_skills, employer_url,
  description, source, source_id, api_url, employer_id, injections,
  fetched_ts)`
- `url_cache(url_hash PK, url, last_scraped_ts, status_code)`
- `skill_categories`, `skills`, `skill_aliases`, `pending_skills`,
  `pending_skill_vacancies`

Full schema reference: `docs/DB.md`.

### Files under `--root`

- `companies/<slug>/exec_dossier.md` — OSINT dossier (`--save-osint`).
- `companies/<slug>/vacancies/<vid>/resume.docx` — rendered resume.
- `master_data/resume_template.docx` — created by `--init-template`.

## End-to-end flow

1. `pipeline_ctl --init` (once) → migrate DB (v4, 12 states, WAL), seed 5
   categories.
2. `docx_tool --init-template` (once) → resume template.
3. `ingest --search …` → vacancies as `DISCOVERED`.
4. Loop `pipeline_ctl --next`, perform the returned `action`, then
   `pipeline_ctl --update-vacancy … --status …`. Repeat until `IDLE`.
5. `sanitize` cleans raw text before any LLM reads it; `skills_tool --check`
   gates skill asks.
6. `docx_tool --render` then `--verify DOCX --vacancy ID` gate the final
   resume; `passed == true` is the QA gate before `QA_PASSED`.

Or run it all at once: `python -m scripts.demo_tool --dry-run`.

## Maintenance

This file restates behavior; the tests are the source of truth. Before
changing a tool, run `python -m pytest tests -q` and update this file if a
flag, JSON key, or error code moved.
