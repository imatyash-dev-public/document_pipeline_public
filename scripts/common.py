"""Shared helpers for the document pipeline CLI toolbelt.

Stdlib only. Library functions never read the clock: callers pass ``now``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CACHE_TTL_DAYS = 90
VAULT_KEYS = (
    "confirmed_skills",
    "rusty_skills",
    "learnable_80h_whitelist",
    "confirmed_missing",
)
GENERIC_KEY = "generic_competencies"
ALIASES_KEY = "aliases"
PENDING_FILE = "pending_skills.json"


class CliError(Exception):
    """An expected, user-facing failure reported as a JSON error object."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    """Return the current UTC time in ``TS_FORMAT``."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(ts: str) -> datetime:
    """Parse a ``TS_FORMAT`` timestamp into an aware UTC datetime."""
    try:
        return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise CliError("invalid_timestamp", f"bad timestamp: {ts}")


def days_between(earlier: str, later: str) -> float:
    """Return the number of days from ``earlier`` to ``later``."""
    return (parse_ts(later) - parse_ts(earlier)).total_seconds() / 86400


def is_fresh(ts: str | None, now: str) -> bool:
    """True when ``ts`` is set and younger than ``CACHE_TTL_DAYS`` days."""
    if ts is None:
        return False
    return days_between(ts, now) < CACHE_TTL_DAYS


def emit(obj: dict) -> None:
    """Print one compact JSON object to stdout."""
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def emit_error(code: str, message: str) -> None:
    """Print one compact JSON error object to stderr."""
    print(
        json.dumps(
            {"error": code, "message": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )


def setup_stdio() -> None:
    """Force UTF-8 on the standard streams when supported."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def run(handler: Callable[[], dict]) -> int:
    """Run ``handler`` and emit its result as the CLI JSON contract."""
    setup_stdio()
    try:
        result = handler()
    except CliError as e:
        emit_error(e.code, e.message)
        return 1
    emit(result)
    return 0


def add_common_args(parser: argparse.ArgumentParser, db: bool = True) -> None:
    """Add the shared ``--root`` / ``--now`` (and optionally ``--db``) options."""
    parser.add_argument("--root", default=".")
    parser.add_argument("--now", default=None)
    if db:
        parser.add_argument("--db", default=None)


def resolve_root(args: argparse.Namespace) -> Path:
    """Resolve the ``--root`` base directory."""
    return Path(args.root).resolve()


def resolve_db(args: argparse.Namespace) -> Path:
    """Resolve the SQLite database path."""
    if args.db:
        return Path(args.db)
    return resolve_root(args) / "db" / "state.sqlite"


def resolve_now(args: argparse.Namespace) -> str:
    """Return ``--now`` after validation, or the current UTC time."""
    if args.now:
        parse_ts(args.now)
        return args.now
    return utc_now()


def rel_path(path: Path | str, root: Path) -> str:
    """Return ``path`` relative to ``root`` with forward slashes."""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(path).resolve().as_posix()


def url_hash(url: str) -> str:
    """Return the deterministic SHA-256 hex digest of a URL."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


TRANSLIT: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def slugify(name: str) -> str:
    """Build a lowercase ASCII slug from a company name."""
    s = "".join(TRANSLIT.get(ch, ch) for ch in name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:60].strip("-")
    return s or "company"


def validate_slug(slug: str) -> str:
    """Validate a company slug, returning it unchanged."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", slug):
        raise CliError("invalid_slug", slug)
    return slug


def validate_vacancy_id(vid: str) -> str:
    """Validate a vacancy id, returning it unchanged."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", vid):
        raise CliError("invalid_vacancy_id", vid)
    return vid


def normalize_term(s: str) -> str:
    """Normalize a skill term for case/whitespace/yo-insensitive matching."""
    t = unicodedata.normalize("NFKC", s).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", t).strip()


DEFAULT_GENERIC_COMPETENCIES: list[str] = [
    normalize_term(s)
    for s in (
        "Аналитическое мышление",
        "Аналитический склад ума",
        "Системное мышление",
        "Аналитика",
        "Анализ данных",
        "Анализ бизнес показателей",
        "Аналитические исследования",
        "Математический анализ",
        "Работа с большим объемом информации",
        "Бизнес-процессы",
        "Автоматизация",
        "Автоматизация процессов",
        "Информационные технологии",
        "IT",
        "Проектная документация",
        "Постановка ТЗ",
        "Разработка технических заданий",
        "Постановка задач разработчикам",
        "Разработка бизнес-требований",
        "Анализ требований",
        "Разработка нефункциональных требований",
        "Разработка пользовательской документации",
        "Сбор требований",
        "Приемочное тестирование",
        "Формирование аналитической отчетности",
        "План-факт анализ",
        "SWOT-анализ",
        "Трекер задач",
        "Управление командой",
        "Управление стейкхолдерами",
        "СУБД",
    )
]

DEFAULT_ALIASES: dict[str, str] = {
    normalize_term("ms excel"): normalize_term("excel"),
    normalize_term("ms sql server"): normalize_term("ms sql"),
    normalize_term("mssql"): normalize_term("ms sql"),
    normalize_term("rest"): normalize_term("rest api"),
    normalize_term("postgres"): normalize_term("postgresql"),
    normalize_term("powerbi"): normalize_term("power bi"),
    normalize_term("apache kafka"): normalize_term("kafka"),
    normalize_term("apache airflow"): normalize_term("airflow"),
}


def apply_alias(term: str, aliases: dict[str, str]) -> str:
    """Return the canonical normalized term after applying ``aliases``."""
    key = normalize_term(term)
    if key in aliases:
        return normalize_term(aliases[key])
    return key


def read_json(path: Path) -> object:
    """Read and parse a UTF-8 JSON file, mapping failures to ``CliError``."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise CliError("file_not_found", str(path))
    except json.JSONDecodeError:
        raise CliError("invalid_json", str(path))


def write_json(path: Path, obj: object) -> None:
    """Atomically write ``obj`` as pretty UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def vault_path(root: Path) -> Path:
    """Return the skills vault path under ``root``."""
    return root / "master_data" / "skills_vault.json"


def pending_path(root: Path) -> Path:
    """Return the pending skill-question queue path under ``root``."""
    return root / "master_data" / PENDING_FILE


def init_vault(root: Path) -> bool:
    """Create an empty vault when missing; return True if it was created."""
    path = vault_path(root)
    if path.exists():
        return False
    vault: dict = {k: {} for k in VAULT_KEYS}
    vault[GENERIC_KEY] = {
        name: {"name": name, "note": "", "added_ts": None}
        for name in DEFAULT_GENERIC_COMPETENCIES
    }
    vault[ALIASES_KEY] = dict(DEFAULT_ALIASES)
    write_json(path, vault)
    return True