"""Runtime settings, all overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # Where the chatterbox-tts server is listening. The deployed container
    # publishes 8004 on the host.
    chatterbox_url: str = os.environ.get("CHATTERBOX_URL", "http://localhost:8004")

    data_dir: Path = Path(os.environ.get("AUDIOBOOK_DATA_DIR", ROOT / "data"))

    host: str = os.environ.get("AUDIOBOOK_HOST", "127.0.0.1")
    port: int = int(os.environ.get("AUDIOBOOK_PORT", "8005"))

    # A single chunk is a few seconds of speech; the ceiling is generous so a
    # slow cold start does not kill an otherwise healthy job.
    tts_timeout_sec: float = float(os.environ.get("AUDIOBOOK_TTS_TIMEOUT", "600"))
    tts_retries: int = int(os.environ.get("AUDIOBOOK_TTS_RETRIES", "3"))

    # The model sits in VRAM whether or not anything is narrating, so this app
    # loads it when a job (or a voice preview) actually needs it and releases
    # it again once nothing does. Set 0 to leave the TTS server alone - useful
    # if you also drive it from its own UI on :8004.
    manage_engine: bool = os.environ.get("AUDIOBOOK_MANAGE_ENGINE", "1") not in ("0", "false", "no")
    # Grace period before an idle model is unloaded. Reloading costs ~11s, so a
    # couple of minutes avoids paying that between back-to-back jobs.
    engine_idle_sec: float = float(os.environ.get("AUDIOBOOK_ENGINE_IDLE_SEC", "180"))
    # Generous: the very first load of a model downloads weights.
    engine_load_timeout_sec: float = float(os.environ.get("AUDIOBOOK_ENGINE_LOAD_TIMEOUT", "600"))

    # Target characters per TTS request. Chatterbox can split text itself, but
    # we chunk up front so progress, resume and retry work per chunk.
    chunk_chars: int = int(os.environ.get("AUDIOBOOK_CHUNK_CHARS", "240"))

    # Silence inserted when stitching, in milliseconds.
    gap_between_chunks_ms: int = int(os.environ.get("AUDIOBOOK_CHUNK_GAP_MS", "220"))
    gap_between_chapters_ms: int = int(os.environ.get("AUDIOBOOK_CHAPTER_GAP_MS", "1100"))

    # Chapters shorter than this are usually title/copyright pages.
    min_chapter_chars: int = int(os.environ.get("AUDIOBOOK_MIN_CHAPTER_CHARS", "300"))

    sample_rate: int = 24000  # chatterbox turbo output rate
    m4b_bitrate: str = os.environ.get("AUDIOBOOK_M4B_BITRATE", "64k")

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "books"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    def ensure_dirs(self) -> None:
        for d in (self.books_dir, self.jobs_dir, self.samples_dir):
            d.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
