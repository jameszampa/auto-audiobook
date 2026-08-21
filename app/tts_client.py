"""Client for the chatterbox-tts server.

Endpoint paths and field names below are taken from the server's own
server.py / models.py (CustomTTSRequest), not inferred.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, asdict

import httpx

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


@dataclass
class VoiceParams:
    """Generation knobs exposed by /tts."""

    temperature: float = 0.8
    exaggeration: float = 0.5   # calmer than the server default; suits narration
    cfg_weight: float = 0.5
    speed_factor: float = 1.0
    # A fixed non-zero seed keeps timbre stable across thousands of chunks.
    # Seed 0 means "random" to the engine, which makes a book drift.
    seed: int = 12345
    language: str = "en"

    def as_dict(self) -> dict:
        return asdict(self)


class ChatterboxClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 600.0,
        retries: int = 3,
        load_timeout: float = 600.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.load_timeout = load_timeout

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def model_info(self) -> dict:
        async with await self._client() as c:
            r = await c.get("/api/model-info", timeout=15)
            r.raise_for_status()
            return r.json()

    async def engine_state(self) -> dict:
        """Reachability and residency, reported separately.

        An unloaded model is a normal idle state here, not a fault: voices,
        uploads and everything else on the server keep working without it.
        """
        try:
            info = await self.model_info()
        except Exception as e:
            return {
                "reachable": False,
                "loaded": False,
                "detail": f"cannot reach chatterbox at {self.base_url}: {e}",
            }
        if not info.get("loaded"):
            return {"reachable": True, "loaded": False, "detail": "model unloaded"}
        return {
            "reachable": True,
            "loaded": True,
            "detail": f"{info.get('class_name', '?')} on {info.get('device', '?')}",
        }

    async def load(self) -> str:
        """Load the model named in the server's config into VRAM.

        /restart_server is the server's model hot-swap: it drops whatever is
        resident and loads from config, and is the only way back after
        /api/unload. It returns once the model is actually loaded (measured
        ~11s for chatterbox-turbo on this box), so no polling is needed.
        """
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.load_timeout
        ) as c:
            r = await c.post("/restart_server")
        if r.status_code != 200:
            raise TTSError(f"model load failed (HTTP {r.status_code}): {r.text[:200]}")
        try:
            return r.json().get("message", "loaded")
        except Exception:
            return "loaded"

    async def unload(self) -> None:
        """Drop the model and release its VRAM. Takes ~3s."""
        async with await self._client() as c:
            r = await c.post("/api/unload", timeout=120)
        if r.status_code != 200:
            raise TTSError(
                f"model unload failed (HTTP {r.status_code}): {r.text[:200]}"
            )

    async def list_reference_files(self) -> list[str]:
        """Voice-cloning reference clips available on the server."""
        async with await self._client() as c:
            r = await c.get("/get_reference_files", timeout=15)
            r.raise_for_status()
            return r.json()

    async def list_predefined_voices(self) -> list[dict]:
        async with await self._client() as c:
            r = await c.get("/get_predefined_voices", timeout=15)
            r.raise_for_status()
            return r.json()

    async def upload_reference(self, filename: str, data: bytes) -> None:
        """Add a new voice-clone wav to the server's reference_audio folder."""
        async with await self._client() as c:
            r = await c.post(
                "/upload_reference",
                files={"files": (filename, data, "audio/wav")},
                timeout=120,
            )
            if r.status_code != 200:
                raise TTSError(f"upload failed ({r.status_code}): {r.text[:300]}")

    async def synth(
        self,
        text: str,
        voice_mode: str,
        voice_id: str,
        params: VoiceParams,
    ) -> bytes:
        """Synthesize one chunk and return WAV bytes.

        ``voice_mode`` is "clone" (voice_id is a reference wav filename) or
        "predefined" (voice_id is a built-in voice filename).
        """
        payload: dict = {
            "text": text,
            "voice_mode": voice_mode,
            "output_format": "wav",
            # We already chunked; letting the server split again would blur the
            # mapping between a request and a resumable unit of work.
            "split_text": False,
            **params.as_dict(),
        }
        if voice_mode == "clone":
            payload["reference_audio_filename"] = voice_id
        else:
            payload["predefined_voice_id"] = voice_id

        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                async with await self._client() as c:
                    r = await c.post("/tts", json=payload)
                if r.status_code == 200 and r.content[:4] == b"RIFF":
                    return r.content
                if r.status_code == 200:
                    last_error = "response was not a RIFF/WAV payload"
                else:
                    last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"

            if attempt < self.retries:
                backoff = 2 ** attempt
                log.warning("TTS attempt %d/%d failed (%s); retrying in %ds",
                            attempt, self.retries, last_error, backoff)
                await asyncio.sleep(backoff)

        raise TTSError(f"synthesis failed after {self.retries} attempts - {last_error}")
