"""CPU integration checks for the bridge contract and audiobook pipeline.

python -m unittest discover -s tests -p test_editx.py -v
"""

import asyncio
import io
import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

from app import main
from app.audio import build_m4b
from app.config import Settings
from app.engine import EngineLifecycle
from app.jobs import Job, JobManager
from app.tts_client import StepAudioEditXClient, TTSError, VoiceParams
from tts_server import server
from tts_server.worker import validate_audio_tokens


def tone(seconds=4):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i * 2 * math.pi * 220 / 24000)))
                                for i in range(int(seconds * 24000))))
    return stream.getvalue()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.patcher = patch.object(server, "VOICE_DIR", self.directory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = TestClient(server.app)
        self.audio = tone()

    def upload(self, transcript="A calm and clear reference recording."):
        return self.client.post("/api/voices/upload", files={"file": ("Narrator.wav", self.audio)},
                                data={"transcript": transcript})

    def test_idle_health_has_engine_identity(self):
        response = self.client.get("/api/model-info")
        self.assertEqual(response.json()["engine"], "step-audio-editx")
        self.assertFalse(response.json()["loaded"])

    def test_voice_upload_stores_transcript_and_pcm(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        voice_id = response.json()["id"]
        self.assertEqual(server.read_voice(voice_id)["transcript"], "A calm and clear reference recording.")
        with wave.open(str(self.directory / f"{voice_id}.wav")) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
        self.assertEqual(self.client.get("/api/voices").json(), [response.json()])

    def test_transcript_changes_voice_identity(self):
        self.assertNotEqual(self.upload("One.").json()["id"], self.upload("Two.").json()["id"])

    def test_missing_empty_transcript_and_bad_audio_rejected(self):
        self.assertEqual(self.client.post("/api/voices/upload", files={"file": ("voice.wav", self.audio)}).status_code, 422)
        self.assertEqual(self.upload("  ").status_code, 400)
        self.audio = b"not audio"
        self.assertEqual(self.upload().status_code, 400)

    def test_too_short_reference_rejected(self):
        self.audio = tone(1)
        self.assertEqual(self.upload().status_code, 400)

    def test_payload_validation_and_missing_voice(self):
        for payload in ({"text": "a", "voice_id": "../voice"},
                        {"text": "a" * 301, "voice_id": "a" * 64},
                        {"text": "a", "voice_id": "a" * 64, "temperature": 9}):
            self.assertEqual(self.client.post("/tts", json=payload).status_code, 422)
        self.assertEqual(self.client.post("/tts", json={"text": "hello", "voice_id": "a" * 64}).status_code, 404)

    def test_synthesis_passes_exact_reference_and_sampling(self):
        voice_id = self.upload().json()["id"]
        fake = Mock(loaded=True)
        fake.lock = __import__("threading").Lock()
        fake.receive.return_value = {"audio": self.audio}
        with patch.object(server, "worker", fake):
            response = self.client.post("/tts", json={"text": "Hello there.", "voice_id": voice_id,
                                                       "temperature": 0.65, "seed": 0})
        self.assertEqual(response.content, self.audio)
        sent = fake.connection.send.call_args.args[0]
        self.assertEqual(sent["transcript"], "A calm and clear reference recording.")
        self.assertEqual(sent["temperature"], 0.65)
        self.assertEqual(sent["seed"], 0)
        self.assertTrue(Path(sent["reference_path"]).exists())

    def test_unload_refuses_active_inference(self):
        with server.worker.lock:
            self.assertEqual(self.client.post("/api/unload").status_code, 409)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler):
        client = StepAudioEditXClient("http://editx", retries=2)
        client._client = AsyncMock(side_effect=lambda: httpx.AsyncClient(
            base_url="http://editx", transport=httpx.MockTransport(handler)))
        return client

    async def test_rejects_wrong_engine(self):
        client = self.make_client(lambda r: httpx.Response(200, json={"loaded": True}))
        self.assertFalse((await client.engine_state())["reachable"])

    async def test_retry_then_wav(self):
        calls = []
        audio = tone(0.2)
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=audio)
        client = self.make_client(handler)
        with patch("app.tts_client.asyncio.sleep", new_callable=AsyncMock):
            result = await client.synth("Hello.", "clone", "a" * 64, VoiceParams())
        self.assertEqual(result, audio)
        self.assertEqual(calls[0], {"text": "Hello.", "voice_id": "a" * 64, "temperature": 0.7, "seed": 12345})

    async def test_bad_request_is_not_retried(self):
        handler = Mock(return_value=httpx.Response(422, text="invalid"))
        client = self.make_client(handler)
        with self.assertRaises(TTSError):
            await client.synth("Hello.", "clone", "bad", VoiceParams())
        self.assertEqual(handler.call_count, 1)

    async def test_engine_lease_loads_then_unloads(self):
        client = Mock()
        client.engine_state = AsyncMock(side_effect=[{"reachable": True, "loaded": False},
                                                      {"reachable": True, "loaded": True}])
        client.load, client.unload = AsyncMock(), AsyncMock()
        engine = EngineLifecycle(client, idle_sec=0.01)
        async with engine.leased("test"):
            self.assertTrue(engine.busy)
            self.assertFalse((await engine.unload_now())["unloaded"])
        await asyncio.sleep(0.05)
        client.load.assert_awaited_once()
        client.unload.assert_awaited_once()
        engine._stand_down()


class AppTests(unittest.TestCase):
    def test_full_dual_codebook_range(self):
        validate_audio_tokens([65536, 66559, 66560, 70655])
        for ids in ([], [3], [65535], [70656]):
            with self.assertRaises(ValueError):
                validate_audio_tokens(ids)

    def test_legacy_resume_does_not_mutate_job(self):
        manager = JobManager(Mock(), Mock())
        job = Job({"status": "completed", "params": {"cfg_weight": 0.5}}, Path("unused"))
        with self.assertRaisesRegex(ValueError, "previous TTS engine"):
            manager.resume(job)
        self.assertEqual(job.status, "completed")

    def test_preview_cache_includes_text_and_sampling(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(data_dir=Path(temporary))
            settings.ensure_dirs()
            engine = EngineLifecycle(Mock(), enabled=False)
            engine.acquire = AsyncMock(return_value=False)
            client = Mock(synth=AsyncMock(return_value=tone(0.2)))
            with patch.object(main, "SETTINGS", settings), patch.object(main, "engine", engine), patch.object(main, "client", client):
                api = TestClient(main.app)
                base = {"mode": "clone", "voice_id": "a" * 64, "text": "Hello."}
                for fields in (base, base, {**base, "text": "Another sentence."}, {**base, "temperature": "0.6"}):
                    self.assertEqual(api.post("/api/voices/sample", data=fields).status_code, 200)
                self.assertEqual(client.synth.await_count, 3)

    def test_form_sampling_validation(self):
        api = TestClient(main.app)
        response = api.post("/api/voices/sample", data={"mode": "clone", "voice_id": "a" * 64,
                                                       "temperature": "NaN"})
        self.assertEqual(response.status_code, 422)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_m4b_normalization_preserves_chapters_and_rate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapters = [root / "one.wav", root / "two.wav"]
            for chapter in chapters:
                chapter.write_bytes(tone())
            output = root / "book.m4b"
            result = await build_m4b(chapters, ["One", "Two"], output, root / "work",
                                     "Test", "Author", 1100, 24000)
            info = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-of", "json", str(output)]))
            self.assertEqual(len(info["chapters"]), 2)
            self.assertAlmostEqual(float(info["chapters"][1]["start_time"]), 5.1, places=2)
            audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
            self.assertEqual((audio["codec_name"], audio["sample_rate"], audio["channels"]), ("aac", "24000", 1))
            self.assertAlmostEqual(result["duration_sec"], 9.1, places=2)


if __name__ == "__main__":
    unittest.main()
