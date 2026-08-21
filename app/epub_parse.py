"""Turn an .epub into ordered, narratable chapters."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

# EbookLib 0.18 warns about future defaults on every read; nothing actionable.
warnings.filterwarnings("ignore", category=UserWarning, module="ebooklib.epub")
warnings.filterwarnings("ignore", category=FutureWarning, module="ebooklib.epub")

# epub content is XHTML; bs4 flags that on every chapter. The HTML parser is
# the deliberate choice here because real-world epubs are often malformed.
try:
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:  # older bs4 without this warning class
    pass

# Markup that should never be spoken.
_DROP_TAGS = ["script", "style", "sup", "sub", "figure", "figcaption", "table", "nav"]

# Front/back matter we default to excluding, matched against the chapter title.
_SKIP_TITLE = re.compile(
    r"^\s*(cover|title\s*page|copyright|colophon|dedication|epigraph|contents|"
    r"table\s+of\s+contents|acknowledge?ments?|about\s+the\s+author|also\s+by|"
    r"index|bibliography|notes?|footnotes?|endnotes?|imprint|praise)\b",
    re.I,
)


@dataclass
class Chapter:
    index: int
    title: str
    text: str
    include: bool = True

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class Book:
    title: str
    author: str
    chapters: list[Chapter] = field(default_factory=list)


def _clean_html(html: bytes | str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()

    # Block elements become newlines so the chunker can see paragraph breaks.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"]):
        block.append("\n")

    text = soup.get_text()
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _heading_title(html: bytes | str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for level in ("h1", "h2", "h3", "title"):
        node = soup.find(level)
        if node:
            title = re.sub(r"\s+", " ", node.get_text()).strip()
            if title:
                return title[:120]
    return None


def _toc_titles(book: epub.EpubBook) -> dict[str, str]:
    """Map spine href -> human title using the book's table of contents."""
    titles: dict[str, str] = {}

    def walk(entries) -> None:
        for entry in entries:
            if isinstance(entry, tuple):
                section, children = entry[0], entry[1]
                if getattr(section, "href", None):
                    titles.setdefault(section.href.split("#")[0], section.title)
                walk(children)
            elif isinstance(entry, epub.Link):
                titles.setdefault(entry.href.split("#")[0], entry.title)

    try:
        walk(book.toc)
    except Exception:  # a malformed TOC must not sink the whole parse
        pass
    return {k: re.sub(r"\s+", " ", (v or "")).strip() for k, v in titles.items()}


def _metadata(book: epub.EpubBook, key: str, fallback: str) -> str:
    try:
        found = book.get_metadata("DC", key)
        if found and found[0][0]:
            return re.sub(r"\s+", " ", found[0][0]).strip()
    except Exception:
        pass
    return fallback


def parse_epub(path: Path, min_chapter_chars: int = 300) -> Book:
    """Read ``path`` and return its chapters in spine (reading) order."""
    book = epub.read_epub(str(path))
    toc = _toc_titles(book)

    chapters: list[Chapter] = []
    for spine_id, _linear in book.spine:
        item = book.get_item_with_id(spine_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        raw = item.get_content()
        text = _clean_html(raw)
        if not text:
            continue

        href = item.get_name().split("#")[0]
        title = toc.get(href) or _heading_title(raw) or f"Chapter {len(chapters) + 1}"

        chapter = Chapter(index=len(chapters), title=title, text=text)
        # Short sections and named front matter start unchecked; the user can
        # re-enable any of them in the UI before starting the run.
        chapter.include = (
            chapter.char_count >= min_chapter_chars and not _SKIP_TITLE.match(title)
        )
        chapters.append(chapter)

    return Book(
        title=_metadata(book, "title", path.stem),
        author=_metadata(book, "creator", "Unknown"),
        chapters=chapters,
    )
