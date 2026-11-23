"""Zero-leak scanner for the public portfolio release.

Fails the build if any prohibited pattern (proprietary scraping target,
anti-bot evasion, real PII, or hardcoded token) appears in repository
source. This file excludes itself so its own pattern literals never match.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
TEXT_SUFFIXES = {
    ".py", ".md", ".json", ".txt", ".yml", ".yaml",
    ".toml", ".cfg", ".ini", ".jsonc", ".code-workspace",
}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}

EMAIL_RE = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
PHONE_RE = r"\+7[\s(]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}"
TOKEN_RES = [
    r"sk-[A-Za-z0-9]{16,}",
    r"Bearer\s+[A-Za-z0-9._-]{8,}",
    r"(?i)(api[_-]?key|apikey|secret|password|token)\s*[:=]\s*[\"'][^\"']{6,}[\"']",
]

FIXTURES = REPO / "tests" / "fixtures"


def iter_source_files(exclude_fixtures: bool) -> Iterator[Path]:
    """Yield repository text files, optionally excluding synthetic fixtures."""
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == SELF:
            continue
        # plan.md is the gitignored working plan, not part of the shipped tree;
        # it deliberately lists the prohibited terms as audit findings.
        if path.resolve() == (REPO / "plan.md").resolve():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if exclude_fixtures and FIXTURES in path.parents:
            continue
        yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_no_proprietary_scraping_targets() -> None:
    prohibited = [
        "hh.ru", "api.hh", "r.jina.ai", "jina", "captcha_required",
        "капча", "ddg-", "check_js", "ddos", "hh-lux", "initialstate",
        "data-qa", "hh_min_delay", "hh_max_delay", "_human_sleep",
        "cookiejar", "browser_ua",
    ]
    offenders: list[tuple[Path, str]] = []
    for path in iter_source_files(exclude_fixtures=False):
        text = _read(path).lower()
        for term in prohibited:
            if term.lower() in text:
                offenders.append((path, term))
    assert offenders == [], (
        "Prohibited scraping/anti-bot terms found:\n"
        + "\n".join(f"  {_rel(path)}: {term}" for path, term in offenders)
    )


def test_no_real_pii_in_tracked_files() -> None:
    matches: list[tuple[Path, str, str]] = []
    for path in iter_source_files(exclude_fixtures=True):
        text = _read(path)
        for value in re.findall(EMAIL_RE, text):
            matches.append((path, "email", value))
        for value in re.findall(PHONE_RE, text):
            matches.append((path, "phone", value))
    assert matches == [], (
        "Real PII patterns found outside tests/fixtures:\n"
        + "\n".join(f"  {_rel(path)}: {kind} {value!r}" for path, kind, value in matches)
    )


def test_no_hardcoded_tokens() -> None:
    matches: list[tuple[Path, str]] = []
    for path in iter_source_files(exclude_fixtures=True):
        text = _read(path)
        for pattern in TOKEN_RES:
            for value in re.findall(pattern, text):
                matches.append((path, f"{pattern} -> {value!r}"))
    assert matches == [], (
        "Hardcoded credential/token patterns found:\n"
        + "\n".join(f"  {_rel(path)}: {detail}" for path, detail in matches)
    )


def test_sensitive_dirs_absent() -> None:
    sensitive = [
        "master_data", "companies", "db", "ReadyToSend",
        "ReadyToSend.7z", ".kilo", "vacancy_processing_report.json",
    ]
    for name in sensitive:
        assert not (REPO / name).exists(), f"Sensitive path exists at repo root: {name}"


def test_fixtures_are_synthetic() -> None:
    allowed_domains = ("example.com", "example.org", "acme.ru", "example.net")
    bad_emails: list[tuple[Path, str]] = []
    bad_phones: list[tuple[Path, str]] = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file():
            continue
        text = _read(path)
        for value in re.findall(EMAIL_RE, text):
            domain = value.rsplit("@", 1)[-1].lower()
            if not domain.endswith(allowed_domains):
                bad_emails.append((path, value))
        for value in re.findall(PHONE_RE, text):
            bad_phones.append((path, value))
    assert not bad_emails, (
        "Fixture emails use non-synthetic domains:\n"
        + "\n".join(f"  {_rel(path)}: {value}" for path, value in bad_emails)
    )
    assert not bad_phones, (
        "Fixture files contain Russian phone patterns:\n"
        + "\n".join(f"  {_rel(path)}: {value}" for path, value in bad_phones)
    )