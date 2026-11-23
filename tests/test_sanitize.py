"""Tests for scripts.sanitize: hidden markup, invisible chars and injections."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.sanitize import INJECTION_PLACEHOLDER, sanitize_text

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "injection_page.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_hidden_css_removed() -> None:
    """CSS-hidden blocks disappear while the visible vacancy text remains."""
    text = sanitize_text(_fixture_html())["text"]
    assert "СКРЫТО" not in text
    assert "Требуется Python-разработчик" in text


def test_script_style_comments_removed() -> None:
    """Script, style and HTML comment contents never reach the text."""
    text = sanitize_text(_fixture_html())["text"]
    assert "СКРИПТ" not in text
    assert "СТИЛЬ" not in text
    assert "КОММЕНТ" not in text


def test_invisible_chars_removed() -> None:
    """Zero-width and bidi characters are stripped and counted."""
    result = sanitize_text(_fixture_html())
    text = result["text"]
    for ch in ("\u200b", "\u200d", "\ufeff", "\u202e"):
        assert ch not in text, f"invisible char U+{ord(ch):04X} survived"
    assert result["removed_invisible"] >= 4
    assert "Зарплата" in text


def test_english_injections_redacted() -> None:
    """English injection phrases are replaced and reported by pattern id."""
    result = sanitize_text(_fixture_html())
    text = result["text"]
    assert "ignore_previous" in result["injections"]
    assert "system_prompt" in result["injections"]
    assert "ignore all previous instructions" not in text.lower()
    assert "system prompt" not in text.lower()
    assert INJECTION_PLACEHOLDER in text


def test_russian_injections_redacted() -> None:
    """Russian injection phrases are replaced and reported by pattern id."""
    result = sanitize_text(_fixture_html())
    text = result["text"]
    assert "ru_forget_previous" in result["injections"]
    assert "ru_you_are_ai" in result["injections"]
    assert "ru_system_prompt" in result["injections"]
    assert "Забудь предыдущие инструкции" not in text
    assert "системный промпт" not in text


def test_benign_text_untouched() -> None:
    """Ordinary Russian text mentioning CI and prompts is not altered."""
    source = "Опыт работы с системами CI и промптами для отчётов"
    result = sanitize_text(source)
    assert result["injections"] == []
    assert result["text"] == source


def test_cli_stdin_roundtrip() -> None:
    """The CLI reads the fixture from stdin and emits one compact JSON line."""
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.sanitize"],
        input=_fixture_html(),
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.strip().splitlines()) == 1
    payload = json.loads(proc.stdout)
    assert "Требуется Python-разработчик" in payload["text"]
    assert "WARNING" in proc.stderr
