"""Split chapter prose into TTS-sized chunks on sentence boundaries.

Chunking here rather than letting the TTS server do it means every chunk is an
independently retryable, resumable unit of work, and progress is reportable at
chunk granularity. For a multi-hour book that matters a lot.
"""

from __future__ import annotations

import re

# Abbreviations whose trailing period is not a sentence end. Without this the
# splitter breaks "Mr. Darcy" into two sentences and the TTS pauses mid-name.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "rev", "hon",
    "capt", "col", "gen", "lt", "sgt", "maj", "adm", "gov", "sen", "rep",
    "vs", "etc", "eg", "ie", "al", "inc", "ltd", "co", "corp", "dept",
    "fig", "vol", "no", "pp", "ca", "approx", "ave", "blvd", "rd",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}

# End of sentence: ., ! or ? plus any closing quotes/brackets, then whitespace.
_SENTENCE_END = re.compile(r'([.!?]+["\'’”)\]]*)(\s+)')

# A single initial like "J." in "J. R. R. Tolkien".
_INITIAL = re.compile(r'\b[A-Z]$')


def split_sentences(text: str) -> list[str]:
    """Break text into sentences, tolerating common abbreviations."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        head = text[start:match.end(1)]
        # Look at the word carrying the period to decide if this is a real stop.
        last_word = re.split(r'[\s(\["\']+', head.strip())[-1]
        bare = last_word.rstrip('."\'’”)]').lower()
        if bare in _ABBREVIATIONS or _INITIAL.search(bare.upper()):
            continue
        # A following lowercase word means the stop was internal to the
        # sentence — usually a period inside quoted dialogue, as in
        # He said "stop. now." Splitting there would pause mid-quote.
        nxt = text[match.end():match.end() + 1]
        if nxt and nxt.islower():
            continue
        sentences.append(head.strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def _split_long_sentence(sentence: str, max_chars: int) -> list[str]:
    """Break a single over-long sentence at clause boundaries, then whitespace."""
    if len(sentence) <= max_chars:
        return [sentence]

    parts: list[str] = []
    remaining = sentence
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        # Prefer a clause break, then any space, then a hard cut.
        cut = max(window.rfind("; "), window.rfind(", "), window.rfind("—"))
        if cut < max_chars // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_chars
        else:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return [p for p in parts if p]


def chunk_text(text: str, max_chars: int = 240) -> list[str]:
    """Group sentences into chunks of at most ``max_chars`` characters."""
    chunks: list[str] = []
    current = ""

    for paragraph in (p.strip() for p in text.split("\n") if p.strip()):
        for sentence in split_sentences(paragraph):
            for piece in _split_long_sentence(sentence, max_chars):
                if not current:
                    current = piece
                elif len(current) + 1 + len(piece) <= max_chars:
                    current = f"{current} {piece}"
                else:
                    chunks.append(current)
                    current = piece
        # Paragraph boundary: flush so a chunk never straddles a scene break.
        if current:
            chunks.append(current)
            current = ""

    if current:
        chunks.append(current)
    return chunks
