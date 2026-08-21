"""Stitch generated chunks into chapters and a chapterised .m4b audiobook."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import wave
from pathlib import Path

log = logging.getLogger(__name__)


class AudioError(RuntimeError):
    pass


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise AudioError(
            f"command failed ({proc.returncode}): {shlex.join(cmd)}\n"
            f"{err.decode(errors='replace')[-800:]}"
        )


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def is_valid_wav(path: Path, min_frames: int = 1) -> bool:
    """Used on resume to decide whether an existing chunk can be trusted."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() >= min_frames
    except Exception:
        return False


async def make_silence(path: Path, ms: int, sample_rate: int) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    await _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{ms / 1000:.3f}",
        "-c:a", "pcm_s16le",
        str(path),
    ])
    return path


def _concat_list(files: list[Path], list_path: Path) -> Path:
    # The concat demuxer needs single quotes escaped as '\''.
    lines = []
    for f in files:
        escaped = str(f.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


async def concat_wavs(files: list[Path], out_path: Path, work_dir: Path) -> Path:
    """Concatenate same-format wavs losslessly."""
    if not files:
        raise AudioError(f"nothing to concatenate for {out_path.name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    list_path = _concat_list(files, work_dir / f"{out_path.stem}.concat.txt")
    await _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", str(out_path),
    ])
    list_path.unlink(missing_ok=True)
    return out_path


def _ffmetadata(title: str, author: str, chapters: list[tuple[str, float, float]]) -> str:
    """Build an ffmetadata document with chapter markers (milliseconds)."""
    def esc(v: str) -> str:
        for ch in ("\\", "=", ";", "#", "\n"):
            v = v.replace(ch, "\\" + ch if ch != "\n" else " ")
        return v

    lines = [";FFMETADATA1", f"title={esc(title)}", f"artist={esc(author)}",
             f"album={esc(title)}", "genre=Audiobook"]
    for name, start, end in chapters:
        lines += [
            "", "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={int(start * 1000)}",
            f"END={int(end * 1000)}",
            f"title={esc(name)}",
        ]
    return "\n".join(lines) + "\n"


async def build_m4b(
    chapter_files: list[Path],
    chapter_titles: list[str],
    out_path: Path,
    work_dir: Path,
    title: str,
    author: str,
    gap_ms: int,
    sample_rate: int,
    bitrate: str = "64k",
    cover: Path | None = None,
) -> dict:
    """Join chapter wavs with gaps and encode a chapterised .m4b."""
    if len(chapter_files) != len(chapter_titles):
        raise AudioError("chapter file/title count mismatch")

    work_dir.mkdir(parents=True, exist_ok=True)
    gap = await make_silence(work_dir / f"gap_{gap_ms}.wav", gap_ms, sample_rate)
    gap_sec = gap_ms / 1000.0

    ordered: list[Path] = []
    marks: list[tuple[str, float, float]] = []
    cursor = 0.0
    for i, (f, name) in enumerate(zip(chapter_files, chapter_titles)):
        if i:
            ordered.append(gap)
            cursor += gap_sec
        start = cursor
        cursor += wav_duration(f)
        ordered.append(f)
        marks.append((name, start, cursor))

    joined = work_dir / "book.wav"
    await concat_wavs(ordered, joined, work_dir)

    meta_path = work_dir / "chapters.ffmeta"
    meta_path.write_text(_ffmetadata(title, author, marks), encoding="utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(joined),
        "-i", str(meta_path),
    ]
    if cover and cover.exists():
        cmd += ["-i", str(cover)]

    cmd += ["-map", "0:a", "-map_metadata", "1"]
    if cover and cover.exists():
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    cmd += ["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart", "-f", "mp4",
            str(out_path)]

    await _run(cmd)
    total = cursor
    joined.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)

    return {
        "duration_sec": total,
        "chapters": [
            {"title": n, "start_sec": round(s, 3), "end_sec": round(e, 3)}
            for n, s, e in marks
        ],
        "size_bytes": out_path.stat().st_size,
    }


async def encode_mp3(src_wav: Path, out_path: Path, bitrate: str = "128k") -> Path:
    await _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src_wav), "-c:a", "libmp3lame", "-b:a", bitrate, str(out_path),
    ])
    return out_path


def write_json(path: Path, payload: dict) -> None:
    """Atomic write so a crash mid-save cannot corrupt job state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
