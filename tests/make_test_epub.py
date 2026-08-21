"""Generate a small .epub for testing the pipeline end to end.

Includes a copyright page and a contents page so the front-matter skipping
logic has something real to act on.

    python tests/make_test_epub.py [out.epub]
"""

from __future__ import annotations

import sys
from pathlib import Path

from ebooklib import epub

CHAPTERS = [
    (
        "Copyright",
        "<p>Copyright © 2026. All rights reserved. No part of this publication "
        "may be reproduced without permission.</p>",
    ),
    (
        "Contents",
        "<p>Chapter One<br/>Chapter Two<br/>Chapter Three</p>",
    ),
    (
        "Chapter One: The Lamplighter",
        "<p>The lamplighter made his round at dusk, touching each wick in turn "
        "until the whole street glowed amber. Mr. Havelock watched him from the "
        "window of the bookshop, as he had every evening for eleven years.</p>"
        "<p>It was, he thought, the only reliable thing left in the city. The "
        "trams changed their routes, the newspapers changed their politics, and "
        "the river changed its colour with every season. But at dusk, without "
        "fail, the lamps came on from east to west.</p>",
    ),
    (
        "Chapter Two: A Letter Arrives",
        "<p>She had promised to write every week, and for a year she did. Then "
        "the letters thinned to nothing, the way a river thins in August until "
        "only stones remain.</p>"
        "<p>Dr. Alcott brought the last one himself, holding it as though it "
        "might come apart in the wind. He did not stay for tea.</p>",
    ),
    (
        "Chapter Three: The Harbour",
        "<p>Down by the harbour the boats knocked together in the swell, rope "
        "creaking against wet stone. Gulls argued over something unseen beneath "
        "the pier, and the air tasted of salt and diesel.</p>"
        "<p>He counted the masts twice, as if the number might have changed "
        "since morning, and found that it had.</p>",
    ),
]


def build(out_path: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier("auto-audiobook-test-001")
    book.set_title("The Lamplighter's Round")
    book.set_language("en")
    book.add_author("A. Test Author")

    items = []
    for i, (title, body) in enumerate(CHAPTERS):
        chapter = epub.EpubHtml(title=title, file_name=f"chap_{i}.xhtml", lang="en")
        chapter.content = f"<html><body><h1>{title}</h1>{body}</body></html>"
        book.add_item(chapter)
        items.append(chapter)

    book.toc = tuple(items)
    book.spine = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tests/sample-book.epub")
    print(f"wrote {build(target)}")
