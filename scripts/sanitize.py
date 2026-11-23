"""HTML/hidden-element stripping, invisible-character removal and injection redaction."""

import argparse
import html.parser
import logging
import re
import sys
from pathlib import Path

from scripts import common

INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\u180e]")


def remove_invisible(text: str) -> tuple[str, int]:
    """Remove zero-width and other invisible characters from *text*."""
    return INVISIBLE_RE.subn("", text)


DROP_TAGS = {"script", "style", "noscript", "template", "head", "svg", "iframe", "object"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section", "article", "header", "footer", "blockquote", "pre"}

HIDDEN_STYLE_RE = re.compile(r"display:none|visibility:hidden|font-size:0+(?:\.0+)?(?:px|pt|em|rem|%)?(?:;|$|!)|opacity:0+(?:\.0+)?(?:;|$|!)|(?<![-\w])color:(?:#fff(?:fff)?|white|rgba?\(255,255,255(?:,[\d.]+)?\))(?:;|$|!)")


def is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """Return True when *attrs* mark the element as visually hidden."""
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "aria-hidden" and value == "true":
            return True
        if name == "style" and value is not None:
            style = re.sub(r"\s+", "", value.lower())
            if HIDDEN_STYLE_RE.search(style):
                return True
    return False


class _TextExtractor(html.parser.HTMLParser):
    """Collect visible text from HTML, dropping hidden/dropped subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []

    def _skipping(self) -> bool:
        return any(skip for _, skip in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS and not self._skipping():
            self.parts.append("\n")
        if tag not in VOID_TAGS:
            self.stack.append((tag, tag in DROP_TAGS or is_hidden(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS and not self._skipping():
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        index = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                index = i
                break
        if index is not None:
            del self.stack[index:]
        if tag in BLOCK_TAGS and not self._skipping():
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping():
            self.parts.append(data)


def strip_html(raw: str) -> str:
    """Strip HTML tags, comments, scripts and hidden elements."""
    extractor = _TextExtractor()
    extractor.feed(raw)
    extractor.close()
    return "".join(extractor.parts)


def normalize_ws(text: str) -> str:
    """Normalize line endings, whitespace runs and repeated blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    return "\n".join(out).strip()


INJECTION_PLACEHOLDER = "[REDACTED_INJECTION_ATTEMPT]"
log = logging.getLogger("sanitize")

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_previous", re.compile(r"\b(?:ignore|disregard|forget|override)\s+(?:(?:all|any|the|your)\s+)*(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|prompts?|rules|directions|messages)", re.IGNORECASE)),
    ("disregard_all", re.compile(r"\bdisregard\s+(?:all|everything)\b", re.IGNORECASE)),
    ("system_prompt", re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE)),
    ("you_are_ai", re.compile(r"\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|language\s+model|chatgpt|llm)\b", re.IGNORECASE)),
    ("new_instructions", re.compile(r"\bnew\s+instructions?\s*:", re.IGNORECASE)),
    ("chat_tokens", re.compile(r"<\|?(?:system|im_start|im_end)\|?>|\[/?INST\]", re.IGNORECASE)),
    ("ru_forget_previous", re.compile(r"(?:забудь|забудьте|игнорируй|игнорируйте|проигнорируй|не\s+обращай\s+внимания\s+на)\s+(?:(?:все|всё)\s+)?(?:предыдущие|прошлые|прежние|вышеуказанные|предшествующие)\s+(?:инструкции|указания|команды|правила|сообщения)", re.IGNORECASE)),
    ("ru_disregard_all", re.compile(r"(?:игнорируй|забудь)\s+(?:всё|все)\b", re.IGNORECASE)),
    ("ru_system_prompt", re.compile(r"системн(?:ый|ого|ому|ым|ом)\s+промпт\w*", re.IGNORECASE)),
    ("ru_you_are_ai", re.compile(r"\bты\s*[—–-]?\s*(?:ии|искусственный\s+интеллект|нейросеть|языковая\s+модель)\b", re.IGNORECASE)),
    ("ru_new_instructions", re.compile(r"\bновые\s+инструкции\s*:", re.IGNORECASE)),
]


def redact_injections(text: str) -> tuple[str, list[str]]:
    """Replace prompt-injection phrases with a placeholder, in pattern order."""
    hits: list[str] = []
    for pid, pat in INJECTION_PATTERNS:
        text, n = pat.subn(INJECTION_PLACEHOLDER, text)
        hits.extend([pid] * n)
        if n:
            log.warning("redacted %d match(es) of %s", n, pid)
    return text, hits


def sanitize_text(raw: str) -> dict:
    """Sanitize raw HTML/text: invisible chars, HTML, whitespace, injections."""
    t, a = remove_invisible(raw)
    t = strip_html(t)
    t, b = remove_invisible(t)
    t = normalize_ws(t)
    t, hits = redact_injections(t)
    return {
        "text": t,
        "removed_invisible": a + b,
        "injections": hits,
        "chars_in": len(raw),
        "chars_out": len(t),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: sanitize stdin or --input-file."""
    parser = argparse.ArgumentParser(prog="python -m scripts.sanitize")
    parser.add_argument("--input-file")
    parser.add_argument("--output-file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="WARNING %(name)s: %(message)s")

    def handle() -> dict:
        if args.input_file:
            path = Path(args.input_file)
            if not path.is_file():
                raise common.CliError("file_not_found", args.input_file)
            raw = path.read_text(encoding="utf-8")
        else:
            common.setup_stdio()
            raw = sys.stdin.read()
        result = sanitize_text(raw)
        if args.output_file:
            out = Path(args.output_file)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(result["text"], encoding="utf-8")
            return {
                "ok": True,
                "output_file": args.output_file.replace("\\", "/"),
                "removed_invisible": result["removed_invisible"],
                "injections": result["injections"],
                "chars_in": result["chars_in"],
                "chars_out": result["chars_out"],
            }
        return result

    return common.run(handle)


if __name__ == "__main__":
    raise SystemExit(main())
