"""FastAPI app: upload an epub, pick a voice, run, download an .m4b."""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .chunker import chunk_text
from .config import ROOT, SETTINGS
from .engine import EngineLifecycle
from .epub_parse import parse_epub
from .jobs import JobManager, slugify
from .tts_client import ChatterboxClient, TTSError, VoiceParams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("auto-audiobook")

client = ChatterboxClient(
    SETTINGS.chatterbox_url,
    timeout=SETTINGS.tts_timeout_sec,
    retries=SETTINGS.tts_retries,
    load_timeout=SETTINGS.engine_load_timeout_sec,
)
engine = EngineLifecycle(
    client,
    enabled=SETTINGS.manage_engine,
    idle_sec=SETTINGS.engine_idle_sec,
)
manager = JobManager(client, engine)

STATIC_DIR = ROOT / "static"
SAFE_NAME = re.compile(r"^[\w.\- ]+$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    SETTINGS.ensure_dirs()
    manager.load_from_disk()
    manager.start_worker()
    state = await client.engine_state()
    log.info(
        "chatterbox at %s: %s%s",
        SETTINGS.chatterbox_url,
        state["detail"],
        "" if not SETTINGS.manage_engine else
        f" (model managed here: loaded on demand, unloaded after "
        f"{SETTINGS.engine_idle_sec:.0f}s idle)",
    )
    # Nothing is narrating yet, so an already-resident model is idle VRAM.
    engine.start()
    yield
    await engine.shutdown()


app = FastAPI(title="auto-audiobook", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Engine status and voices
# --------------------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict:
    """`reachable` is the health signal; `loaded` is just where the GPU is.

    An idle app deliberately leaves no model in VRAM, so "not loaded" is a
    normal state and must not read as an outage.
    """
    return {**await engine.state(), "url": SETTINGS.chatterbox_url}


@app.post("/api/engine/unload")
async def engine_unload() -> dict:
    """Free the GPU now instead of waiting out the idle timer."""
    try:
        return await engine.unload_now("requested via API")
    except Exception as e:
        raise HTTPException(502, f"unload failed: {e}")


@app.get("/api/voices")
async def voices() -> dict:
    """Clone references first - they are the point of the app."""
    try:
        clone = await client.list_reference_files()
        predefined = await client.list_predefined_voices()
    except Exception as e:
        raise HTTPException(502, f"cannot list voices from chatterbox: {e}")

    return {
        "clone": [{"id": f, "label": Path(f).stem} for f in clone],
        "predefined": [
            {"id": v.get("filename", ""), "label": v.get("display_name") or v.get("filename", "")}
            for v in predefined
        ],
    }


@app.post("/api/voices/upload")
async def upload_voice(file: UploadFile = File(...)) -> dict:
    """Add your own voice-clone wav to the shared list."""
    name = Path(file.filename or "").name
    if not name.lower().endswith((".wav", ".mp3")):
        raise HTTPException(400, "voice reference must be a .wav or .mp3 file")
    if not SAFE_NAME.match(name):
        raise HTTPException(400, "filename may only contain letters, numbers, spaces, . _ -")

    data = await file.read()
    if not data:
        raise HTTPException(400, "uploaded file is empty")
    try:
        await client.upload_reference(name, data)
    except TTSError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "id": name, "label": Path(name).stem}


@app.post("/api/voices/sample")
async def voice_sample(
    mode: str = Form(...),
    voice_id: str = Form(...),
    text: str = Form("This is how the narrator will sound reading your book."),
) -> Response:
    """Short preview so a voice can be judged before committing hours of GPU."""
    if mode not in ("clone", "predefined"):
        raise HTTPException(400, "mode must be 'clone' or 'predefined'")
    if not SAFE_NAME.match(Path(voice_id).name) or Path(voice_id).name != voice_id:
        raise HTTPException(400, "invalid voice id")

    cache = SETTINGS.samples_dir / f"{mode}_{slugify(voice_id)}.wav"
    if not cache.exists():
        # Cached previews are served without ever waking the GPU.
        try:
            async with engine.leased(f"preview {voice_id}"):
                audio = await client.synth(text[:300], mode, voice_id, VoiceParams())
        except TTSError as e:
            raise HTTPException(502, str(e))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(audio)
    return Response(cache.read_bytes(), media_type="audio/wav")


# --------------------------------------------------------------------------
# Books
# --------------------------------------------------------------------------
@app.post("/api/books")
async def upload_book(file: UploadFile = File(...)) -> dict:
    """Store an uploaded epub and return its detected chapters."""
    if not (file.filename or "").lower().endswith(".epub"):
        raise HTTPException(400, "please upload an .epub file")

    book_id = uuid.uuid4().hex
    book_dir = SETTINGS.books_dir / book_id
    book_dir.mkdir(parents=True, exist_ok=True)
    source = book_dir / "source.epub"
    source.write_bytes(await file.read())

    try:
        book = parse_epub(source, SETTINGS.min_chapter_chars)
    except Exception as e:
        raise HTTPException(400, f"could not parse epub: {e}")

    if not book.chapters:
        raise HTTPException(400, "no readable text found in that epub")

    payload = {
        "book_id": book_id,
        "title": book.title,
        "author": book.author,
        "chapters": [
            {
                "index": c.index,
                "title": c.title,
                "chars": c.char_count,
                "include": c.include,
                "preview": c.text[:180],
            }
            for c in book.chapters
        ],
    }
    (book_dir / "parsed.json").write_text(
        json.dumps(
            {**payload, "texts": {str(c.index): c.text for c in book.chapters}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    included = [c for c in payload["chapters"] if c["include"]]
    payload["estimate"] = _estimate(sum(c["chars"] for c in included))
    return payload


def _load_book(book_id: str) -> dict:
    path = SETTINGS.books_dir / book_id / "parsed.json"
    if not path.exists():
        raise HTTPException(404, "unknown book id")
    return json.loads(path.read_text(encoding="utf-8"))


def _estimate(chars: int) -> dict:
    """Rough audio length and runtime.

    ~14.5 chars/second of speech and ~3.8x faster than real-time were both
    measured against an RTX 4090 deployment rather than assumed.
    """
    audio_sec = chars / 14.5
    return {
        "chars": chars,
        "audio_hours": round(audio_sec / 3600, 2),
        "compute_hours": round(audio_sec / 3.8 / 3600, 2),
    }


@app.post("/api/books/{book_id}/estimate")
async def estimate(book_id: str, chapters: str = Form(...)) -> dict:
    book = _load_book(book_id)
    wanted = {int(i) for i in json.loads(chapters)}
    total = sum(len(t) for k, t in book["texts"].items() if int(k) in wanted)
    return _estimate(total)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(
    book_id: str = Form(...),
    voice_mode: str = Form(...),
    voice_id: str = Form(...),
    chapters: str = Form(...),
    exaggeration: float = Form(0.5),
    cfg_weight: float = Form(0.5),
    temperature: float = Form(0.8),
    speed_factor: float = Form(1.0),
    seed: int = Form(12345),
) -> dict:
    book = _load_book(book_id)
    if voice_mode not in ("clone", "predefined"):
        raise HTTPException(400, "voice_mode must be 'clone' or 'predefined'")

    try:
        wanted = sorted({int(i) for i in json.loads(chapters)})
    except Exception:
        raise HTTPException(400, "chapters must be a JSON array of indices")
    if not wanted:
        raise HTTPException(400, "select at least one chapter")

    by_index = {c["index"]: c for c in book["chapters"]}
    plan = []
    for index in wanted:
        meta = by_index.get(index)
        text = book["texts"].get(str(index))
        if meta is None or not text:
            continue
        chunks = chunk_text(text, SETTINGS.chunk_chars)
        if chunks:
            plan.append({"index": index, "title": meta["title"], "chunks": chunks})

    if not plan:
        raise HTTPException(400, "selected chapters contain no narratable text")

    params = VoiceParams(
        temperature=temperature,
        exaggeration=exaggeration,
        cfg_weight=cfg_weight,
        speed_factor=speed_factor,
        seed=seed,
    )
    job = manager.create(
        book_id=book_id,
        title=book["title"],
        author=book["author"],
        chapters=plan,
        voice_mode=voice_mode,
        voice_id=voice_id,
        params=params,
    )
    return job.data


def _get_job(job_id: str):
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job id")
    return job


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    ordered = sorted(
        manager.jobs.values(), key=lambda j: j.data["created_at"], reverse=True
    )
    return [
        {
            k: j.data[k]
            for k in ("id", "title", "author", "status", "created_at", "voice",
                      "progress", "timing", "output", "error")
        }
        for j in ordered
    ]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    return _get_job(job_id).data


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = _get_job(job_id)
    manager.cancel(job)
    return {"ok": True, "status": job.status}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict:
    job = _get_job(job_id)
    if job.status in ("queued", "running", "assembling"):
        raise HTTPException(400, "job is already active")
    manager.resume(job)
    return {"ok": True, "status": job.status}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    output = job.data.get("output")
    if not output:
        raise HTTPException(409, "this job has no finished audiobook yet")
    path = Path(output["path"])
    if not path.exists():
        raise HTTPException(404, "output file is missing from disk")
    return FileResponse(path, media_type="audio/mp4", filename=output["filename"])


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    import shutil

    job = _get_job(job_id)
    if job.status in ("queued", "running", "assembling"):
        raise HTTPException(400, "cancel the job before deleting it")
    shutil.rmtree(job.dir, ignore_errors=True)
    manager.jobs.pop(job_id, None)
    return {"ok": True}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
