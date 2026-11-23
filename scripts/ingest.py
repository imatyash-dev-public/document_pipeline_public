"""Offline, deterministic, pluggable ingestion adapters for the document pipeline.

The module is fully offline: adapters read local fixture files, library
functions never read the clock (``now`` is passed in), and there is no
randomness. ``python -m scripts.ingest`` prints exactly one compact JSON
object.
"""

from __future__ import annotations

import abc
import argparse
import html
import json
import re
import sqlite3
import statistics
from pathlib import Path

from scripts import common
from scripts.db import connect
from scripts.pipeline_ctl import (
    check_url,
    record_url,
    set_company_osint,
    set_vacancy_details,
    upsert_company,
    upsert_vacancy,
)
from scripts.sanitize import redact_injections, remove_invisible, sanitize_text


class BaseIngestionAdapter(abc.ABC):
    """Contract for a vacancy source: search, detail fetch and market scan."""

    @abc.abstractmethod
    def search(
        self,
        query: str,
        area: str,
        per_page: int,
        page: int,
    ) -> dict:
        """Return ``{"found": int, "items": [{"id": str}, ...]}``."""

    @abc.abstractmethod
    def fetch_details(self, source_id: str) -> dict:
        """Return an API-shaped vacancy detail dict for ``source_id``."""

    @abc.abstractmethod
    def market_scan(
        self,
        query: str,
        area: str,
        per_page: int,
        page: int,
    ) -> dict:
        """Return ``{"found": int, "items": [{"name", "salary"}, ...]}``."""


class FixtureIngestionAdapter(BaseIngestionAdapter):
    """Read search, vacancy and market data from a local fixture directory."""

    def __init__(self, source_dir: Path) -> None:
        if not Path(source_dir).is_dir():
            raise common.CliError("source_dir_not_found", str(source_dir))
        self.source_dir = Path(source_dir)

    def search(
        self,
        query: str,
        area: str,
        per_page: int,
        page: int,
    ) -> dict:
        path = self.source_dir / "search" / (common.slugify(query) + ".json")
        if not path.is_file():
            raise common.CliError("fixture_not_found", str(path))
        data = common.read_json(path)
        if not isinstance(data, dict):
            raise common.CliError("fixture_not_found", str(path))
        found = data.get("found")
        items = data.get("items")
        if (
            not isinstance(found, int)
            or isinstance(found, bool)
            or not isinstance(items, list)
        ):
            raise common.CliError("fixture_not_found", str(path))
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise common.CliError("fixture_not_found", str(path))
        return {"found": found, "items": items}

    def fetch_details(self, source_id: str) -> dict:
        json_path = (
            self.source_dir / "vacancies" / f"vacancy_{source_id}.json"
        )
        if json_path.is_file():
            detail = common.read_json(json_path)
            if not isinstance(detail, dict):
                raise common.CliError("fixture_not_found", str(json_path))
            return detail

        html_path = (
            self.source_dir / "vacancies" / f"vacancy_{source_id}.html"
        )
        if html_path.is_file():
            text = html_path.read_text(encoding="utf-8")
            detail = parse_job_html(text)
            if detail is None or not detail.get("name"):
                raise common.CliError("fixture_not_found", str(html_path))
            detail["id"] = source_id
            return detail

        raise common.CliError("fixture_not_found", str(json_path))

    def market_scan(
        self,
        query: str,
        area: str,
        per_page: int,
        page: int,
    ) -> dict:
        slug = common.slugify(query)
        path = self.source_dir / "market" / (slug + ".json")
        if not path.is_file():
            # Also honor the ``market_<slug>.json`` fixture convention.
            alternative = self.source_dir / "market" / ("market_" + slug + ".json")
            if alternative.is_file():
                path = alternative
        if not path.is_file():
            raise common.CliError("fixture_not_found", str(path))
        data = common.read_json(path)
        if not isinstance(data, dict):
            raise common.CliError("fixture_not_found", str(path))
        return data


def parse_job_html(html_text: str) -> dict | None:
    """Parse the first schema.org ``JobPosting`` JSON-LD block, if any."""
    match = re.search(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
        html_text,
        re.S,
    )
    if match is None:
        return None
    try:
        data = json.loads(html.unescape(match.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    employer: dict | None = None
    organization = data.get("hiringOrganization")
    if isinstance(organization, dict) and organization.get("name"):
        employer = {"id": None, "name": organization.get("name")}

    area: dict | None = None
    location = data.get("jobLocation") or {}
    address = location.get("address") if isinstance(location, dict) else None
    locality = (
        address.get("addressLocality") if isinstance(address, dict) else None
    )
    if locality:
        area = {"name": locality}

    skills = data.get("skills")
    if skills is None:
        skills = data.get("key_skills")
    key_skills = [{"name": s} for s in (skills or [])]

    return {
        "id": None,
        "name": data.get("title"),
        "alternate_url": data.get("url"),
        "employer": employer,
        "description": data.get("description"),
        "key_skills": key_skills,
        "area": area,
        "experience": None,
        "salary": None,
        "match_score": 85,
    }


def clean(s: str | None) -> tuple[str, list[str]]:
    """Sanitize a text field, returning its text and injection ids."""
    if s is None:
        return "", []
    r = sanitize_text(s)
    return r["text"], r["injections"]


def build_vacancy_record(
    detail: dict,
    now: str,
    source: str = "fixture",
) -> dict:
    """Build the clean vacancy record from an API-shaped detail dict."""
    employer = detail.get("employer") or {}
    company_name = employer.get("name") or "unknown"

    title, title_hits = clean(detail.get("name"))
    description, description_hits = clean(detail.get("description"))
    hard_skills: list[str] = []
    skill_hits: list[str] = []
    for skill in detail.get("key_skills") or []:
        value = skill.get("name") if isinstance(skill, dict) else skill
        text, hits = clean(value)
        skill_hits.extend(hits)
        if text:
            hard_skills.append(text)

    return {
        "vacancy_id": str(detail["id"]),
        "source": source,
        "source_id": str(detail["id"]),
        "url": detail.get("alternate_url"),
        "api_url": None,
        "title": title,
        "company_name": company_name,
        "company_slug": common.slugify(company_name),
        "employer_id": employer.get("id"),
        "employer_url": employer.get("alternate_url"),
        "area": (detail.get("area") or {}).get("name"),
        "experience": (detail.get("experience") or {}).get("name"),
        "salary": detail.get("salary"),
        "hard_skills": hard_skills,
        "description": description,
        "injections": title_hits + description_hits + skill_hits,
        "fetched_ts": now,
        "match_score": detail.get("match_score"),
    }


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IGNORED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "sentry-next.wixpress.com",
)
IGNORED_EMAIL_EXT = (
    "png",
    "jpg",
    "jpeg",
    "gif",
    "svg",
    "webp",
    "css",
    "js",
)
IGNORED_EMAIL_LOCAL = ("noreply", "no-reply", "donotreply", "mailer-daemon")


def extract_emails(raw: str) -> list[str]:
    """Extract plausible contact emails, dropping generic and asset matches."""
    found: list[str] = []
    for match in EMAIL_RE.findall(raw):
        email = match.lower().rstrip(".")
        local, _, domain = email.partition("@")
        if any(
            domain == ignored or domain.endswith("." + ignored)
            for ignored in IGNORED_EMAIL_DOMAINS
        ):
            continue
        if domain.rsplit(".", 1)[-1] in IGNORED_EMAIL_EXT:
            continue
        if local in IGNORED_EMAIL_LOCAL:
            continue
        if email not in found:
            found.append(email)
    return found


TG_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})\b",
    re.IGNORECASE,
)
IGNORED_TG = ("share", "joinchat", "addstickers", "proxy", "socks")


def extract_telegram(raw: str) -> list[str]:
    """Extract Telegram contact links, dropping shared/utility paths."""
    found: list[str] = []
    for match in TG_RE.finditer(raw):
        name = match.group(1).lower()
        if name in IGNORED_TG:
            continue
        link = "https://t.me/" + name
        if link not in found:
            found.append(link)
    return found


def save_osint(
    conn: sqlite3.Connection,
    root: Path,
    slug: str,
    src: Path,
    now: str,
) -> dict:
    """Store a sanitized Markdown dossier and register its OSINT timestamp."""
    common.validate_slug(slug)
    if not src.is_file():
        raise common.CliError("file_not_found", str(src))
    text = src.read_text(encoding="utf-8")
    t, _ = remove_invisible(text)
    t, hits = redact_injections(t)
    if not t.strip():
        raise common.CliError("empty_dossier", str(src))
    dossier_path = f"companies/{slug}/exec_dossier.md"
    dossier = root / "companies" / slug / "exec_dossier.md"
    dossier.parent.mkdir(parents=True, exist_ok=True)
    dossier.write_text(t, encoding="utf-8")
    row = conn.execute(
        "SELECT 1 FROM companies WHERE company_slug=?",
        (slug,),
    ).fetchone()
    if row is None:
        upsert_company(conn, slug, slug, None)
    set_company_osint(conn, slug, dossier_path, now)
    return {
        "ok": True,
        "company_slug": slug,
        "dossier_path": dossier_path,
        "chars": len(t),
        "last_osint_ts": now,
        "injections": hits,
    }


def salary_midpoint(salary: dict) -> float | None:
    """Return the estimated midpoint of a salary dict, if any range exists."""
    if not salary:
        return None
    low = salary.get("from")
    high = salary.get("to")
    if low is not None and high is not None:
        return (low + high) / 2
    if low is not None:
        return low * 1.2
    if high is not None:
        return high * 0.8
    return None


def _persist_vacancy(
    conn: sqlite3.Connection,
    root: Path,
    rec: dict,
    now: str,
) -> dict:
    """Write a clean vacancy record and register it in SQLite."""
    slug = rec["company_slug"]
    folder = root / "companies" / slug / "vacancies" / rec["vacancy_id"]
    folder.mkdir(parents=True, exist_ok=True)
    upsert_company(conn, slug, rec["company_name"], None)
    upsert_vacancy(
        conn,
        rec["vacancy_id"],
        slug,
        rec["title"],
        rec["url"],
        common.rel_path(folder, root),
        now,
    )
    set_vacancy_details(
        conn,
        rec["vacancy_id"],
        rec.get("area"),
        rec.get("experience"),
        rec.get("salary"),
        rec.get("hard_skills"),
        rec.get("employer_url"),
        rec.get("description"),
        now,
        source=rec.get("source"),
        source_id=rec.get("source_id"),
        api_url=rec.get("api_url"),
        employer_id=rec.get("employer_id"),
        injections=rec.get("injections"),
        fetched_ts=rec.get("fetched_ts"),
    )
    return {
        "vacancy_id": rec["vacancy_id"],
        "company_slug": slug,
        "folder_path": common.rel_path(folder, root),
    }


def search_and_persist(
    conn: sqlite3.Connection,
    root: Path,
    adapter: BaseIngestionAdapter,
    query: str,
    area: str,
    per_page: int,
    page: int,
    now: str,
) -> dict:
    """Search through ``adapter`` and persist every new vacancy."""
    data = adapter.search(query, area, per_page, page)
    saved: list[dict] = []
    skipped_cached: list[str] = []

    for item in data.get("items", []):
        source_id = str(item["id"])
        if check_url(conn, source_id, now)["result"] == "CACHE_SKIP":
            skipped_cached.append(source_id)
            continue
        detail = adapter.fetch_details(source_id)
        rec = build_vacancy_record(detail, now)
        saved.append(_persist_vacancy(conn, root, rec, now))
        record_url(conn, source_id, now, 200)

    return {
        "ok": True,
        "query": query,
        "found": data.get("found", 0),
        "saved": saved,
        "skipped_cached": skipped_cached,
        "errors": [],
    }


def market_scan(
    adapter: BaseIngestionAdapter,
    query: str,
    area: str,
    per_page: int,
    page: int,
) -> dict:
    """Summarize one market page: counts, currencies and RUR salary stats."""
    data = adapter.market_scan(query, area, per_page, page)
    items = data.get("items") or []
    with_salary = 0
    currencies: dict[str, int] = {}
    midpoints: list[float] = []
    sample_titles: list[str] = []
    injections = 0

    for item in items:
        title, hits = clean(item.get("name"))
        injections += len(hits)
        if len(sample_titles) < 10:
            sample_titles.append(title)
        salary = item.get("salary")
        if not isinstance(salary, dict):
            continue
        if salary.get("from") is None and salary.get("to") is None:
            continue
        with_salary += 1
        code = salary.get("currency") or "unknown"
        currencies[code] = currencies.get(code, 0) + 1
        if code != "RUR":
            continue
        midpoint = salary_midpoint(salary)
        if midpoint is not None:
            midpoints.append(midpoint)

    salary_stats: dict | None = None
    if midpoints:
        if len(midpoints) == 1:
            median = p25 = p75 = low = high = midpoints[0]
        else:
            median = statistics.median(midpoints)
            quantiles = statistics.quantiles(midpoints, n=4, method="inclusive")
            p25 = quantiles[0]
            p75 = quantiles[2]
            low = min(midpoints)
            high = max(midpoints)
        salary_stats = {
            "median": median,
            "p25": p25,
            "p75": p75,
            "min": low,
            "max": high,
            "currency": "RUR",
            "sample": len(midpoints),
        }

    return {
        "ok": True,
        "query": query,
        "area": area,
        "found": data.get("found", 0),
        "scanned": len(items),
        "with_salary": with_salary,
        "without_salary": len(items) - with_salary,
        "currencies": currencies,
        "salary": salary_stats,
        "sample_titles": sample_titles,
        "injections": injections,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the ingestion CLI."""
    parser = argparse.ArgumentParser(prog="ingest")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--search", metavar="QUERY")
    group.add_argument("--market-scan", metavar="QUERY")
    group.add_argument("--save-osint", metavar="SLUG")

    parser.add_argument(
        "--source-dir",
        default="tests/fixtures/synthetic_sources",
    )
    parser.add_argument("--area", default="113")
    parser.add_argument("--per-page", type=int, default=20)
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--company")
    parser.add_argument("--file")

    common.add_common_args(parser, db=True)
    return parser


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict:
    """Resolve paths and dispatch the requested ingestion action."""
    root = common.resolve_root(args)
    db_path = common.resolve_db(args)
    now = common.resolve_now(args)
    adapter = FixtureIngestionAdapter(Path(args.source_dir))

    if args.market_scan:
        return market_scan(
            adapter, args.market_scan, args.area, args.per_page, args.page
        )

    conn = connect(db_path)
    try:
        if args.search:
            return search_and_persist(
                conn,
                root,
                adapter,
                args.search,
                args.area,
                args.per_page,
                args.page,
                now,
            )
        if args.save_osint:
            if not args.file:
                parser.error("--save-osint requires --file")
            return save_osint(
                conn,
                root,
                args.save_osint,
                Path(args.file),
                now,
            )
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
