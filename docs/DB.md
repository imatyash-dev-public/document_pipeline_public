# Database reference — document pipeline state layer

Reference for LLM agents that query the SQLite state layer. Read this instead
of `scripts/db.py`. The behavior is pinned by `python -m pytest tests -q`;
the tests win on conflict.

## Location & invariants

- File: `<root>/db/state.sqlite` (default root `.`). Gitignored — never
  committed. Created on first `connect()`.
- `PRAGMA user_version` = **4** (schema version).
- `PRAGMA journal_mode` = **WAL** (enabled on every connection).
- Every connection runs `PRAGMA foreign_keys = ON` and returns
  `sqlite3.Row` rows.
- Standard library `sqlite3` only; parameterized queries only.
- Timestamps are ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`), stored as TEXT.
- Paths stored in the DB are relative to `--root` with forward slashes.

## Tables

### `companies`

One row per employer/company.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `company_slug` | TEXT | PRIMARY KEY | lowercase ASCII slug (`common.slugify`) |
| `company_name` | TEXT | NOT NULL | display name |
| `domain` | TEXT | | site hostname (optional) |
| `dossier_path` | TEXT | | `companies/<slug>/exec_dossier.md` |
| `last_osint_ts` | TEXT | | ISO stamp set by `--save-osint` |

### `vacancies`

One row per vacancy.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `vacancy_id` | TEXT | PRIMARY KEY | source id (e.g. fixture id `1001`) |
| `company_slug` | TEXT | NOT NULL, FK→`companies(company_slug)` | |
| `title` | TEXT | NOT NULL | vacancy title |
| `url` | TEXT | | vacancy page URL |
| `match_score` | INTEGER | | 0..100, set by `--update-vacancy --score` |
| `status` | TEXT | NOT NULL, DEFAULT `DISCOVERED`, CHECK | 12 statuses below |
| `qa_attempts` | INTEGER | NOT NULL, DEFAULT 0 | bumped on each `QA_FAILED_*` |
| `folder_path` | TEXT | | `companies/<slug>/vacancies/<id>` |
| `created_ts` | TEXT | NOT NULL | ISO |
| `updated_ts` | TEXT | NOT NULL | ISO |
| `wave` | INTEGER | nullable | dispatch batch 1..3 (`--set-wave`) |
| `result` | TEXT | nullable | free-form outcome (`--set-result`) |
| `area` | TEXT | nullable | location |
| `experience` | TEXT | nullable | required experience |
| `salary` | TEXT | nullable | JSON object `{"from","to","currency","gross"}` |
| `hard_skills` | TEXT | nullable | JSON array of strings |
| `employer_url` | TEXT | nullable | employer page URL |
| `description` | TEXT | nullable | sanitized vacancy description |
| `source` | TEXT | nullable | ingestion source tag (e.g. `fixture`) |
| `source_id` | TEXT | nullable | source-specific id |
| `api_url` | TEXT | nullable | source API URL (nullable in the offline release) |
| `employer_id` | TEXT | nullable | source employer id |
| `injections` | TEXT | nullable | JSON array of injection ids |
| `fetched_ts` | TEXT | nullable | ISO ingestion timestamp |

### `url_cache`

Scrape cache for the 90-day freshness rule.

| Column | Type | Constraints |
|---|---|---|
| `url_hash` | TEXT | PRIMARY KEY (SHA-256 hex of the URL/source id) |
| `url` | TEXT | NOT NULL |
| `last_scraped_ts` | TEXT | NOT NULL |
| `status_code` | INTEGER | |

### `skill_categories`

Skill categories (classes) for the resume. Seeded with 5 owner categories;
order is `sort_order`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `category_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT | NOT NULL, UNIQUE | |
| `sort_order` | INTEGER | NOT NULL | display order |

### `skills`

One row per known skill term (any claim bucket). `normalized` is the
`common.normalize_term` key (case/whitespace/yo-insensitive).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `normalized` | TEXT | PRIMARY KEY | normalized skill key |
| `name` | TEXT | NOT NULL | display name |
| `category_id` | INTEGER | FK→`skill_categories(category_id)` ON DELETE SET NULL | resume class; NULL = uncategorized |
| `bucket` | TEXT | NOT NULL, CHECK in `confirmed`,`rusty`,`learnable`,`missing`,`generic` | claim bucket |
| `note` | TEXT | NOT NULL DEFAULT `''` | owner note |
| `added_ts` | TEXT | nullable | ISO |

### `skill_aliases`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `alias` | TEXT | PRIMARY KEY | normalized alias term |
| `canonical` | TEXT | NOT NULL | normalized canonical term |

### `pending_skills`

Unknown terms awaiting an owner answer (the human-in-the-loop queue).

| Column | Type | Constraints |
|---|---|---|
| `normalized` | TEXT | PRIMARY KEY |
| `name` | TEXT | NOT NULL |
| `first_seen` | TEXT | NOT NULL |

### `pending_skill_vacancies`

Which vacancies each pending term was seen in.

| Column | Type | Constraints |
|---|---|---|
| `normalized` | TEXT | NOT NULL, FK→`pending_skills(normalized)` ON DELETE CASCADE |
| `vacancy_id` | TEXT | NOT NULL, FK→`vacancies(vacancy_id)` ON DELETE CASCADE |
| | | PRIMARY KEY (`normalized`, `vacancy_id`) |

### Indexes

- `idx_vacancies_status` on `vacancies(status)`.
- `idx_skills_category` on `skills(category_id)`.
- `idx_skills_bucket` on `skills(bucket)`.

## Statuses (`VACANCY_STATUSES`, 12)

`DISCOVERED`, `SKIPPED_LOW_MATCH`, `OSINT_READY`,
`AWAITING_SKILL_CLARIFICATION`, `PLAN_READY`, `DRAFT_READY`,
`QA_FAILED_WRITER`, `QA_FAILED_STRATEGY`, `QA_PASSED`, `DISPATCHED`,
`HALTED_MAX_RETRIES`, `CLOSED`.

Terminal (excluded from `--next`): `SKIPPED_LOW_MATCH`, `QA_PASSED`,
`HALTED_MAX_RETRIES`, `DISPATCHED`, `CLOSED`.

## Schema history

- v1: `companies`, `vacancies` (core columns), `url_cache`.
- v2: `vacancies` detail columns (`wave`, `result`, `area`, `experience`,
  `salary`, `hard_skills`, `employer_url`, `description`).
- v3: skill tables + `vacancies` source columns (`source`, `source_id`,
  `api_url`, `employer_id`, `injections`, `fetched_ts`).
- v4: 12-status FSM (adds `DISPATCHED`, `CLOSED`) + WAL.

## Read/write entry points

- `python -m scripts.pipeline_ctl --get-vacancy ID` — full structured record
  (all `vacancies` columns + `company_name`; JSON columns parsed).
- `python -m scripts.pipeline_ctl --set-wave ID --wave N` — dispatch wave.
- `python -m scripts.pipeline_ctl --set-result ID --result "…"` — outcome.
- `python -m scripts.pipeline_ctl --summary` — batch table.
- `python -m scripts.pipeline_ctl --next` — the single next pipeline action.

See `docs/CLI.md` for flags, exact JSON shapes, and error codes.

## DB ↔ files relationship

- `companies/<slug>/exec_dossier.md` (per company) holds the authored OSINT
  prose; only its path and timestamp live in `companies`.
- `master_data/resume_template.docx` is the render template (created by
  `docx_tool --init-template`).
- Authored artifacts (`resume_payload.json`, `resume.docx`, QA feedback)
  are files, not DB rows.
