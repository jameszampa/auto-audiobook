"""Job manager: one book at a time, resumable, cancellable.

A full-length book is hours of GPU work, so every chunk is written to disk as
it is produced. Restarting the app (or the machine) loses at most one chunk.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio import (
    AudioError,
    build_m4b,
    concat_wavs,
    is_valid_wav,
    make_silence,
    write_json,
)
from .config import SETTINGS
from .engine import EngineLifecycle
from .tts_client import ChatterboxClient, TTSError, VoiceParams

log = logging.getLogger(__name__)

ACTIVE_STATES = {"queued", "running", "assembling"}
LOG_LINES = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "audiobook") -> str:
    slug = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return (slug[:80] or fallback).lower()


class Job:
    def __init__(self, data: dict[str, Any], directory: Path):
        self.data = data
        self.dir = directory
        self.cancel_event = asyncio.Event()

    # ---- paths -----------------------------------------------------------
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def json_path(self) -> Path:
        return self.dir / "job.json"

    @property
    def plan_path(self) -> Path:
        return self.dir / "plan.json"

    @property
    def chunks_dir(self) -> Path:
        return self.dir / "chunks"

    @property
    def chapters_dir(self) -> Path:
        return self.dir / "chapters"

    @property
    def work_dir(self) -> Path:
        return self.dir / "work"

    # ---- state -----------------------------------------------------------
    def save(self) -> None:
        self.data["updated_at"] = _now()
        write_json(self.json_path, self.data)

    def set_status(self, status: str, error: str | None = None) -> None:
        self.data["status"] = status
        if error:
            self.data["error"] = error
        self.save()

    def log_line(self, message: str) -> None:
        entry = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        self.data.setdefault("log", []).append(entry)
        del self.data["log"][:-LOG_LINES]
        log.info("[job %s] %s", self.id[:8], message)


class JobManager:
    def __init__(self, client: ChatterboxClient, engine: EngineLifecycle):
        self.client = client
        self.engine = engine
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------
    def load_from_disk(self) -> None:
        SETTINGS.ensure_dirs()
        for job_dir in sorted(SETTINGS.jobs_dir.glob("*/")):
            path = job_dir / "job.json"
            if not path.exists():
                continue
            try:
                import json
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("skipping unreadable job at %s: %s", path, e)
                continue
            job = Job(data, job_dir)
            # Anything mid-flight when the process died is resumable, not lost.
            if job.status in ACTIVE_STATES:
                job.data["status"] = "interrupted"
                job.log_line("Server restarted while this job was running.")
                job.save()
            self.jobs[job.id] = job

    def start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if job is None:
                self.queue.task_done()
                continue
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one bad job kill the worker
                log.exception("job %s crashed", job_id)
                job.log_line(f"FAILED: {e}")
                job.set_status("failed", str(e))
            finally:
                self.queue.task_done()

    # ---- submission ------------------------------------------------------
    def create(
        self,
        book_id: str,
        title: str,
        author: str,
        chapters: list[dict],
        voice_mode: str,
        voice_id: str,
        params: VoiceParams,
    ) -> Job:
        job_id = uuid.uuid4().hex
        directory = SETTINGS.jobs_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)

        chunk_total = sum(len(c["chunks"]) for c in chapters)
        char_total = sum(len(t) for c in chapters for t in c["chunks"])

        write_json(directory / "plan.json", {"chapters": chapters})

        data = {
            "id": job_id,
            "book_id": book_id,
            "title": title,
            "author": author,
            "voice": {"mode": voice_mode, "id": voice_id},
            "params": params.as_dict(),
            "status": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
            "progress": {
                "chunks_done": 0,
                "chunks_total": chunk_total,
                "chapters_total": len(chapters),
                "chars_total": char_total,
                "current_chapter_index": None,
                "current_chapter_title": None,
                "audio_sec": 0.0,
            },
            "timing": {"started_at": None, "elapsed_sec": 0.0, "eta_sec": None},
            "output": None,
            "log": [],
        }
        job = Job(data, directory)
        job.log_line(
            f"Queued '{title}' - {len(chapters)} chapters, {chunk_total} chunks, "
            f"voice {voice_id} ({voice_mode})."
        )
        job.save()
        self.jobs[job_id] = job
        self.queue.put_nowait(job_id)
        self.start_worker()
        return job

    def resume(self, job: Job) -> None:
        job.cancel_event = asyncio.Event()
        job.data["error"] = None
        job.set_status("queued")
        job.log_line("Resuming - existing chunks will be reused.")
        job.save()
        self.queue.put_nowait(job.id)
        self.start_worker()

    def cancel(self, job: Job) -> None:
        job.cancel_event.set()
        if job.status == "queued":
            job.log_line("Cancelled before it started.")
            job.set_status("cancelled")

    # ---- the actual work -------------------------------------------------
    async def _process(self, job: Job) -> None:
        import json

        if job.cancel_event.is_set():
            job.set_status("cancelled")
            return

        # Reachability only: an unloaded model is loaded on demand below, and
        # a resume with every chunk already on disk never needs it at all.
        engine_state = await self.client.engine_state()
        if not engine_state["reachable"]:
            job.log_line(f"FAILED: {engine_state['detail']}")
            job.set_status("failed", engine_state["detail"])
            return

        plan = json.loads(job.plan_path.read_text(encoding="utf-8"))
        chapters: list[dict] = plan["chapters"]

        job.chunks_dir.mkdir(parents=True, exist_ok=True)
        job.chapters_dir.mkdir(parents=True, exist_ok=True)
        job.work_dir.mkdir(parents=True, exist_ok=True)

        params = VoiceParams(**job.data["params"])
        mode = job.data["voice"]["mode"]
        voice_id = job.data["voice"]["id"]

        job.data["timing"]["started_at"] = _now()
        job.set_status("running")
        started = time.monotonic()
        done = 0
        reused = 0
        total = job.data["progress"]["chunks_total"]

        # The lease is taken on the first chunk that really has to be
        # synthesized and dropped as soon as the last one is, so the model is
        # in VRAM only while this job is generating audio - not while it
        # replays cached chunks and not during ffmpeg assembly.
        async with AsyncExitStack() as engine_lease:
            leased = False

            for chapter in chapters:
                ci = chapter["index"]
                job.data["progress"]["current_chapter_index"] = ci
                job.data["progress"]["current_chapter_title"] = chapter["title"]
                job.log_line(f"Chapter {ci + 1}/{len(chapters)}: {chapter['title']}")
                job.save()

                for qi, text in enumerate(chapter["chunks"]):
                    if job.cancel_event.is_set():
                        job.log_line("Cancelled by user.")
                        job.set_status("cancelled")
                        return

                    out = job.chunks_dir / f"ch{ci:04d}_{qi:05d}.wav"
                    if is_valid_wav(out):
                        done += 1
                        reused += 1
                    else:
                        if not leased:
                            job.log_line("Reserving the TTS model ...")
                            job.save()
                            try:
                                loaded_now = await engine_lease.enter_async_context(
                                    self.engine.leased(f"job {job.id[:8]}")
                                )
                            except Exception as e:
                                job.log_line(f"FAILED: could not load the TTS model: {e}")
                                job.set_status("failed", str(e))
                                return
                            leased = True
                            job.log_line(
                                "TTS model loaded." if loaded_now
                                else "TTS model was already loaded."
                            )

                        try:
                            audio = await self.client.synth(text, mode, voice_id, params)
                        except TTSError as e:
                            job.log_line(f"FAILED on chapter {ci + 1} chunk {qi + 1}: {e}")
                            job.set_status("failed", str(e))
                            return
                        out.write_bytes(audio)
                        done += 1

                    elapsed = time.monotonic() - started
                    fresh = max(done - reused, 1)
                    remaining = total - done
                    # Rate is measured on freshly generated chunks only, so a resume
                    # that skips thousands of cached chunks does not report a wild ETA.
                    eta = (elapsed / fresh) * remaining if remaining else 0.0

                    progress = job.data["progress"]
                    progress["chunks_done"] = done
                    job.data["timing"]["elapsed_sec"] = round(elapsed, 1)
                    job.data["timing"]["eta_sec"] = round(eta, 1)
                    if done % 5 == 0 or done == total:
                        job.save()

        if reused:
            job.log_line(f"Reused {reused} chunks already on disk.")

        await self._assemble(job, chapters)

    async def _assemble(self, job: Job, chapters: list[dict]) -> None:
        job.set_status("assembling")
        job.log_line("Stitching chapters and encoding .m4b ...")

        gap = await make_silence(
            job.work_dir / f"gap_{SETTINGS.gap_between_chunks_ms}.wav",
            SETTINGS.gap_between_chunks_ms,
            SETTINGS.sample_rate,
        )

        chapter_files: list[Path] = []
        chapter_titles: list[str] = []
        for chapter in chapters:
            ci = chapter["index"]
            pieces: list[Path] = []
            for qi in range(len(chapter["chunks"])):
                wav = job.chunks_dir / f"ch{ci:04d}_{qi:05d}.wav"
                if not is_valid_wav(wav):
                    raise AudioError(f"missing chunk {wav.name}; resume the job")
                if pieces:
                    pieces.append(gap)
                pieces.append(wav)

            target = job.chapters_dir / f"chapter_{ci:04d}.wav"
            await concat_wavs(pieces, target, job.work_dir)
            chapter_files.append(target)
            chapter_titles.append(chapter["title"])

        filename = f"{slugify(job.data['title'])}.m4b"
        out_path = job.dir / filename
        result = await build_m4b(
            chapter_files=chapter_files,
            chapter_titles=chapter_titles,
            out_path=out_path,
            work_dir=job.work_dir,
            title=job.data["title"],
            author=job.data["author"],
            gap_ms=SETTINGS.gap_between_chapters_ms,
            sample_rate=SETTINGS.sample_rate,
            bitrate=SETTINGS.m4b_bitrate,
        )

        job.data["output"] = {
            "filename": filename,
            "path": str(out_path),
            "duration_sec": round(result["duration_sec"], 1),
            "size_bytes": result["size_bytes"],
            "chapters": result["chapters"],
        }
        job.data["progress"]["audio_sec"] = round(result["duration_sec"], 1)
        hours = result["duration_sec"] / 3600
        job.log_line(
            f"Done - {hours:.2f} h of audio, "
            f"{result['size_bytes'] / 1e6:.1f} MB, {filename}"
        )
        job.set_status("completed")
