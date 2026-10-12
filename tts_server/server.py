"""HTTP contract owned by auto-audiobook, wrapping the pinned upstream model.

Run exactly one uvicorn worker. A lock serializes GPU use and process lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import signal
import subprocess
import tempfile
import threading
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .worker import run_worker

VOICE_DIR = Path(os.environ.get("STEP_AUDIO_VOICES_DIR", "/data/voices"))
VOICE_ID = re.compile(r"^[a-f0-9]{64}$")


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    voice_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    temperature: float = Field(default=0.7, ge=0.1, le=1.2)
    seed: int = Field(default=12345, ge=0, le=2147483647)


class GPUWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.connection = None
        self.ready = False

    @property
    def loaded(self):
        return self.ready and self.process is not None and self.process.is_alive()

    def stop(self):
        # Call with lock held. Kill the process group, including vLLM workers;
        # deleting Python references / empty_cache alone does not release them.
        process = self.process
        if process is not None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    if process.is_alive():
                        process.kill()
            elif process.is_alive():
                process.kill()
            process.join(timeout=10)
        if self.connection is not None:
            self.connection.close()
        self.process = self.connection = None
        self.ready = False

    def receive(self, timeout):
        if not self.connection.poll(timeout):
            self.stop()
            raise HTTPException(504, "Step-Audio-EditX worker timed out; see container logs")
        try:
            result = self.connection.recv()
        except (EOFError, OSError):
            self.stop()
            raise HTTPException(503, "GPU worker exited; see container logs for memory/dependency errors")
        if "error" in result:
            raise HTTPException(500, result["error"])
        return result

    def load(self):
        if self.loaded:
            return
        self.stop()
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=run_worker, args=(child,))
        self.process.start()
        child.close()
        try:
            result = self.receive(float(os.environ.get("STEP_AUDIO_LOAD_TIMEOUT", "1800")))
            self.ready = result.get("ready", False)
            if not self.loaded:
                raise HTTPException(503, "GPU worker failed to become ready")
        except Exception:
            self.stop()
            raise


worker = GPUWorker()


@asynccontextmanager
async def lifespan(app):
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    with worker.lock:
        worker.stop()


app = FastAPI(title="Step-Audio-EditX audiobook bridge", lifespan=lifespan)


@app.get("/api/model-info")
def model_info():
    return {"engine": "step-audio-editx", "loaded": worker.loaded,
            "class_name": "StepAudioTTS", "device": "cuda", "sample_rate": 24000}


@app.post("/api/load")
def load():
    with worker.lock:
        worker.load()
    return {"message": "Step-Audio-EditX loaded"}


@app.post("/api/unload")
def unload():
    if not worker.lock.acquire(blocking=False):
        raise HTTPException(409, "model is in use")
    try:
        worker.stop()
    finally:
        worker.lock.release()
    return {"message": "model unloaded"}


def read_voice(voice_id):
    if not VOICE_ID.fullmatch(voice_id):
        raise HTTPException(400, "invalid reference id")
    metadata = VOICE_DIR / f"{voice_id}.json"
    if not metadata.is_file() or not (VOICE_DIR / f"{voice_id}.wav").is_file():
        raise HTTPException(404, "reference voice not found; upload audio and its transcript")
    return json.loads(metadata.read_text(encoding="utf-8"))


@app.get("/api/voices")
def voices():
    result = []
    for path in sorted(VOICE_DIR.glob("*.json")):
        if VOICE_ID.fullmatch(path.stem) and path.with_suffix(".wav").is_file():
            metadata = read_voice(path.stem)
            result.append({"id": path.stem, "label": metadata["label"]})
    return result


@app.post("/api/voices/upload")
def upload(file: UploadFile = File(...), transcript: str = Form(...)):
    transcript = transcript.strip()
    if not transcript or len(transcript) > 2000:
        raise HTTPException(400, "reference transcript must be 1–2000 characters")
    data = file.file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "reference recording must be smaller than 20 MB")
    if not data:
        raise HTTPException(400, "reference recording is empty")
    # Include transcript in identity so replacing a voice never mixes a book's
    # old and new narration, and previews cannot serve stale reference audio.
    voice_id = hashlib.sha256(data + b"\0" + transcript.encode()).hexdigest()
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=VOICE_DIR) as temporary:
        source, output = Path(temporary) / "input", Path(temporary) / "voice.wav"
        source.write_bytes(data)
        try:
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-t", "31", "-vn", "-ac", "1", "-ar", "24000",
                "-c:a", "pcm_s16le", str(output),
            ], check=True, capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            raise HTTPException(400, f"cannot decode reference recording: {exc}")
        with wave.open(str(output), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
        if not 3 <= duration <= 30:
            raise HTTPException(400, "use 3–30 seconds of clean speech (ideally 8–15 seconds)")
        label = Path((file.filename or "Narrator").replace("\\", "/")).stem[:100]
        metadata = {"label": label, "transcript": transcript, "duration_sec": duration}
        meta = Path(temporary) / "voice.json"
        meta.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        output.replace(VOICE_DIR / f"{voice_id}.wav")
        meta.replace(VOICE_DIR / f"{voice_id}.json")
    return {"id": voice_id, "label": label}


@app.post("/tts")
def synth(request: SynthesisRequest):
    if not request.text.strip():
        raise HTTPException(400, "text must contain speech")
    voice = read_voice(request.voice_id)
    with worker.lock:
        if not worker.loaded:
            raise HTTPException(503, "model unloaded; call /api/load first")
        payload = request.model_dump()
        payload.update(reference_path=str(VOICE_DIR / f"{request.voice_id}.wav"),
                       transcript=voice["transcript"])
        try:
            worker.connection.send(payload)
        except (BrokenPipeError, OSError):
            worker.stop()
            raise HTTPException(503, "GPU worker exited; reload the model")
        result = worker.receive(float(os.environ.get("STEP_AUDIO_SYNTH_TIMEOUT", "540")))
    return Response(result["audio"], media_type="audio/wav")
