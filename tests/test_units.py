"""Unit checks for the pieces that do not need a running TTS server.

    ./.venv/bin/python tests/test_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chunker import chunk_text, split_sentences  # noqa: E402
from app.epub_parse import parse_epub  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


print("== sentence splitting ==")
s = split_sentences("Mr. Havelock went home. He slept well.")
check("abbreviation 'Mr.' does not split the sentence", len(s) == 2, f"got {s}")

s = split_sentences("She paused. Then ran! Did she? Yes.")
check("splits on . ! and ?", len(s) == 4, f"got {s}")

s = split_sentences('He said "stop. now." Then left.')
check("handles quotes", len(s) == 2, f"got {s}")

print("== chunking ==")
text = " ".join(["This is a sentence of moderate length."] * 40)
chunks = chunk_text(text, max_chars=240)
check("every chunk respects the limit", all(len(c) <= 240 for c in chunks),
      f"max={max(len(c) for c in chunks)}")
check("no chunk is empty", all(c.strip() for c in chunks))
check("no text is lost",
      sum(len(c.split()) for c in chunks) == len(text.split()),
      f"{sum(len(c.split()) for c in chunks)} vs {len(text.split())} words")

long_sentence = "word " * 300
chunks = chunk_text(long_sentence, max_chars=200)
check("an over-long sentence is broken up", all(len(c) <= 200 for c in chunks),
      f"max={max(len(c) for c in chunks)}")

check("empty input yields no chunks", chunk_text("   ", 240) == [])

print("== epub parsing ==")
sample = Path(__file__).parent / "sample-book.epub"
if not sample.exists():
    from make_test_epub import build
    build(sample)

book = parse_epub(sample, min_chapter_chars=300)
check("title read from metadata", book.title == "The Lamplighter's Round", book.title)
check("author read from metadata", book.author == "A. Test Author", book.author)
check("all sections found", len(book.chapters) == 5, f"got {len(book.chapters)}")

titles = [c.title for c in book.chapters]
included = [c.title for c in book.chapters if c.include]
check("copyright page excluded by default",
      not any("Copyright" in t for t in included), f"included={included}")
check("contents page excluded by default",
      not any("Contents" in t for t in included), f"included={included}")
check("real chapters included", len(included) == 3, f"included={included}")
check("chapter order preserved",
      titles.index("Chapter One: The Lamplighter") < titles.index("Chapter Three: The Harbour"))

body = next(c for c in book.chapters if c.title.startswith("Chapter One"))
check("heading text is not duplicated into prose",
      body.text.count("The Lamplighter") <= 2, body.text[:120])
check("prose survives cleaning", "lamplighter made his round" in body.text.lower())

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
