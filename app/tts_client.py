"""Client for this repository's Step-Audio-EditX HTTP bridge."""

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
    """Sampling controls implemented by the EditX bridge."""

    temperature: float = 0.7  # upstream sampling default; retain natural variation
    # Reproducible sampling; voice consistency primarily comes from the reference.
    seed: int = 12345

    def as_dict(self) -> dict:
        return asdict(self)


class StepAudioEditXClient:
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
            info = r.json()
            if info.get("engine") != "step-audio-editx":
                raise TTSError("expected the Step-Audio-EditX bridge; check STEP_AUDIO_URL")
            return info

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
                "detail": f"cannot reach Step-Audio-EditX at {self.base_url}: {e}",
            }
        if not info.get("loaded"):
            return {"reachable": True, "loaded": False, "detail": "model unloaded"}
        return {
            "reachable": True,
            "loaded": True,
            "detail": f"{info.get('class_name', '?')} on {info.get('device', '?')}",
        }

    async def load(self) -> str:
        """Start the isolated GPU worker and wait for model readiness."""
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.load_timeout
        ) as c:
            r = await c.post("/api/load")
        if r.status_code != 200:
            raise TTSError(f"model load failed (HTTP {r.status_code}): {r.text[:200]}")
        try:
            return r.json().get("message", "loaded")
        except Exception:
            return "loaded"

    async def unload(self) -> None:
        """Stop the GPU worker, releasing tokenizer, vocoder and vLLM memory."""
        async with await self._client() as c:
            r = await c.post("/api/unload", timeout=120)
        if r.status_code != 200:
            raise TTSError(
                f"model unload failed (HTTP {r.status_code}): {r.text[:200]}"
            )

    async def list_reference_files(self) -> list[dict]:
        """Voice-cloning reference clips available on the server."""
        async with await self._client() as c:
            r = await c.get("/api/voices", timeout=15)
            r.raise_for_status()
            return r.json()

    async def upload_reference(self, filename: str, data: bytes, transcript: str) -> dict:
        """Store an immutable reference recording with its exact transcript."""
        async with await self._client() as c:
            r = await c.post(
                "/api/voices/upload",
                files={"file": (filename, data, "application/octet-stream")},
                data={"transcript": transcript},
                timeout=120,
            )
            if r.status_code != 200:
                raise TTSError(f"upload failed ({r.status_code}): {r.text[:300]}")
            return r.json()

    async def synth(
        self,
        text: str,
        voice_mode: str,
        voice_id: str,
        params: VoiceParams,
    ) -> bytes:
        """Synthesize one chunk and return mono 24 kHz PCM WAV bytes."""
        if voice_mode != "clone":
            raise TTSError("Step-Audio-EditX requires a reference voice and transcript")
        payload: dict = {
            "text": text,
            "voice_id": voice_id,
            **params.as_dict(),
        }

        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                async with await self._client() as c:
                    r = await c.post("/tts", json=payload)
                if r.status_code == 200 and r.content[:4] == b"RIFF" and r.content[8:12] == b"WAVE":
                    return r.content
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    raise TTSError(f"HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code == 200:
                    last_error = "response was not a RIFF/WAV payload"
                else:
                    last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            except TTSError:
                raise
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"

            if attempt < self.retries:
                backoff = 2 ** attempt
                log.warning("TTS attempt %d/%d failed (%s); retrying in %ds",
                            attempt, self.retries, last_error, backoff)
                await asyncio.sleep(backoff)

        raise TTSError(f"synthesis failed after {self.retries} attempts - {last_error}")
