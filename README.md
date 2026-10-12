# auto-audiobook

Turn an EPUB into a chaptered `.m4b` audiobook with a local
[Step-Audio-EditX](https://github.com/stepfun-ai/Step-Audio-EditX) narrator.
The web app runs on port **8005**; the bundled GPU bridge runs on **8006**.
Book text and reference recordings stay local. The first model load downloads
public weights from Hugging Face.

## Quick start

Requirements: Python **3.12** recommended for the web app, `ffmpeg` on PATH,
and Docker with NVIDIA GPU support (Docker Desktop/WSL2 on Windows).
The GPU container uses Linux, Python 3.12, and upstream's locked dependencies;
there is no need to install PyTorch or vLLM in the web app's virtual environment.
Allow substantial disk space for the CUDA image, dependencies and model weights.

```bash
# From this repo, on Windows PowerShell or Linux/WSL:
docker compose up -d --build step-audio-editx
```

Then start the web app:

```powershell
# Windows
uv venv --python 3.12 .venv-win
uv pip install --python .venv-win\Scripts\python.exe -r requirements.txt
.venv-win\Scripts\python.exe serve_win.py
```

```bash
# Linux / WSL (creates the web app virtual environment on first run)
./run.sh
```

Open **http://127.0.0.1:8005**. Upload a reference recording **and its exact
transcript**, preview the narrator, upload your EPUB, select chapters, and run.
An idle, unloaded model is normal. The first preview/run downloads and loads
both `stepfun-ai/Step-Audio-EditX` and `stepfun-ai/Step-Audio-Tokenizer`.
Downloads persist in the `step-audio-models` Docker volume; references live in
`data/step-audio-voices`.

```bash
docker compose logs -f step-audio-editx
# Optional: download/load the model before your first preview
curl -X POST http://127.0.0.1:8006/api/load
```

The bridge rejects other engines, so an old Chatterbox server cannot silently
produce narration. Existing books and completed downloads are preserved.
Old jobs cannot be resumed with EditX: start a new run, and re-upload the desired
voice with its transcript. This avoids mixing engines in one audiobook.

## Natural, clear narration

The reference recording is the strongest control over voice, pace and delivery.
Use **8–15 seconds** of relaxed, expressive narration from one speaker, with
clear diction, steady volume, and no music, clipping, echo or overlapping voices.
The server accepts 3–30 seconds. Enter the words exactly as spoken; do not use
this field for instructions such as “speak naturally.”

Defaults favor faithful narration:

- Full model weights with BF16 inference, without 4-bit quantization.
- Temperature **0.7**, matching upstream's generation default. Seed **12345**
  is passed to both vLLM and the decoder's random generators. A seed makes
  sampling repeatable; it does not guarantee identical delivery across texts
  or different GPU/software versions. Seed zero is also a valid fixed seed.
- Native **24 kHz mono** PCM chunks and **96 kbps AAC** audiobook output.
- Sentence-aware **240-character** chunks, clamped to 80–300 characters,
  aiming to stay below upstream's 30-second inference guidance. Generations
  that hit the token limit or exceed 30 seconds fail rather than saving a
  truncated chunk. Shorten the text/reference if this happens.
- Original speaking pace and prosody. No time stretching, forced emotional
  edits, or added laughs/breaths. These can distract in long-form narration.
- Cached reference tokens and speaker features keep the conditioning consistent
  across a book and avoid repeatedly encoding the same voice.
- Conservative outer-silence trimming with 100 ms padding, 5 ms edge fades
  to reduce clicks, 120 ms added between chunks and 1100 ms between chapters.
- Two-pass whole-book loudness normalization targeting **-20 LUFS**, a
  **-2 dBTP** ceiling and **11 LU** loudness range. This keeps one volume
  profile across chapters; it does not separately boost each quiet phrase.

These are starting defaults, not a guarantee of subjective voice quality.
Preview several passages, including dialogue, before a long run. Preview uses
the current temperature and seed. Lowering temperature slightly may help an
unstable reference, but overly low values can flatten expression. Fixing a noisy
reference or mismatched transcript is usually more useful than changing sampling.

## GPU and lifecycle

The bridge pins upstream source revision
`a652e87052c109e26f616d60971376ff47a829d4` and its `uv.lock`, including the
vLLM build used by that source. The adapter uses upstream `StepAudioTTS.clone()`
and overrides its generation hook to pass the selected temperature and seed,
respect the context limit, and reject truncated or invalid audio tokens.

Defaults budget 40% of GPU memory for vLLM, with a 4096-token context, one
sequence, BF16 vocoder, and CUDA graphs disabled to leave room for the tokenizer
and vocoder. These settings are intended for a 24 GB GPU such as an RTX 4090;
actual fit and throughput depend on other GPU workloads and installed drivers.
Close other GPU workloads if loading reports insufficient VRAM. Do not assume
that increasing vLLM's memory fraction leaves enough room for the other models.
The upstream demo documents a larger GPU configuration. This repo's settings
passed a local RTX 4090 smoke test: two English passages generated as 24 kHz
mono PCM using upstream's test reference, and unloading returned GPU memory
to its pre-test level. This verifies inference, not subjective narration quality.

Only a preview or job loads the GPU worker. After 180 seconds idle, the app
stops its process group, including vLLM's child processes, to release VRAM.
The HTTP bridge and voice list stay available. Use one HTTP worker and one
app instance per bridge. `POST /api/engine/unload` releases an idle model now.
If sharing the bridge with another application, set `AUDIOBOOK_MANAGE_ENGINE=0`
and manage `/api/load` and `/api/unload` yourself.

## Running it as a service

`run.sh` is fine for a foreground run. To keep it up across reboots there are
two deployments in `deploy/`, and they bind the same port — run one or the
other, not both.

**Windows (native, current).** A scheduled task runs `serve_win.py` under
`.venv-win` via `pythonw.exe`, so there is no console window to close by
accident and everything it prints lands in `data/server.log`.

```powershell
uv venv --python 3.12 .venv-win
uv pip install --python .venv-win\Scripts\python.exe -r requirements.txt
powershell -ExecutionPolicy Bypass -File deploy\auto-audiobook.ps1
```

Use **3.12, not 3.14** — `lxml==5.3.0` ships no cp314 wheel, so pip falls back
to a source build and fails looking for `libxml2`.

Two details that are easy to get wrong, both explained in the script:

- It starts **at logon, not at boot.** The TTS server is a Docker Desktop
  container and Docker Desktop itself only starts at logon, so a boot-time
  service comes up first and sits unable to reach `:8006`.
- Restarting a dead server is done with a **1-minute repetition trigger**, not
  Task Scheduler's restart-on-failure, which does not reliably fire when the
  process simply dies. With `MultipleInstances=IgnoreNew` each tick is a no-op
  while the server is healthy.

`deploy\auto-audiobook.ps1 -Uninstall` removes it.

**WSL / systemd (previous).** `deploy/auto-audiobook.service` runs `run.sh`
under a systemd user unit. Replace `REPO` with the checkout path as WSL sees it
(`/mnt/c/...`), then `systemctl --user enable --now auto-audiobook`.

Note that job records store an absolute path to the finished `.m4b`, so books
produced under one deployment are not downloadable from the other until those
paths are rewritten.

## Configuration

Web app environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `STEP_AUDIO_URL` | `http://localhost:8006` | Bundled EditX bridge address |
| `AUDIOBOOK_TTS_TIMEOUT` | `600` | Seconds per synthesis request |
| `AUDIOBOOK_TTS_RETRIES` | `3` | Attempts for transient synthesis failures |
| `AUDIOBOOK_MANAGE_ENGINE` | `1` | Load/unload the GPU worker on demand |
| `AUDIOBOOK_ENGINE_IDLE_SEC` | `180` | Idle time before unloading |
| `AUDIOBOOK_ENGINE_LOAD_TIMEOUT` | `1800` | Model download/load timeout; also used by compose |
| `AUDIOBOOK_HOST` | `127.0.0.1` | Web app bind address |
| `AUDIOBOOK_PORT` | `8005` | Web app port |
| `AUDIOBOOK_CHUNK_CHARS` | `240` | Characters per chunk, clamped to 80–300 |
| `AUDIOBOOK_CHUNK_GAP_MS` | `120` | Added silence between chunks |
| `AUDIOBOOK_CHAPTER_GAP_MS` | `1100` | Silence between chapters |
| `AUDIOBOOK_MIN_CHAPTER_CHARS` | `300` | Shorter sections start unticked |
| `AUDIOBOOK_M4B_BITRATE` | `96k` | AAC bitrate |
| `AUDIOBOOK_DATA_DIR` | `./data` | Books, jobs and previews |
| `AUDIOBOOK_INSTALL_TTS` | `auto` | `run.sh`: ask on terminal; `1` builds local Docker bridge; `0` skips |

Compose environment variables (set before `docker compose up`):

| Variable | Default | Meaning |
|---|---|---|
| `STEP_AUDIO_PORT` | `8006` | Host port; also set `STEP_AUDIO_URL` if changed |
| `STEP_AUDIO_GPU_MEMORY_UTILIZATION` | `0.4` | Fraction reserved for vLLM |
| `STEP_AUDIO_CONTEXT_SIZE` | `4096` | Prompt + output token context |

Advanced bridge-only variables: `STEP_AUDIO_SOURCE` points to the pinned
upstream checkout; `STEP_AUDIO_MODEL_PATH` and `STEP_AUDIO_TOKENIZER_PATH` can
point to pre-downloaded directories mounted inside the container;
`STEP_AUDIO_VOICES_DIR` defaults to `/data/voices`; `STEP_AUDIO_LOAD_TIMEOUT`
defaults to 1800 seconds and `STEP_AUDIO_SYNTH_TIMEOUT` to 540 seconds.
Use a compose override to pass these variables and corresponding mounts.
The plain HTTP bridge is bound to loopback by default.

## Layout and tests

- `app/`: EPUB parsing, sentence chunking, EditX client, job queue, GPU leases,
  resume/cancel, ffmpeg assembly and FastAPI routes.
- `tts_server/`: local HTTP bridge, isolated model worker and Dockerfile.
- `static/`: web UI, transcript uploads and narrator previews.
- `compose.yml`: GPU service with persistent model and reference storage.
- `data/`: uploaded books, references, cached chunks and finished audiobooks.

```powershell
.venv-win\Scripts\python.exe tests/test_units.py
.venv-win\Scripts\python.exe -m unittest discover -s tests -p test_editx.py -v
```

Linux uses `.venv/bin/python` for the same commands. CPU integration tests
exercise the bridge HTTP contract with a mocked GPU worker, actual ffmpeg
reference conversion, cache isolation, lifecycle, retry behavior, legacy-job
protection and normalized M4B chapter assembly. They do not validate GPU
inference or perceptual audio quality. With the real bridge running and a
reference uploaded, `./tests/e2e.sh` narrates the sample EPUB end to end
(requires curl, jq, ffmpeg and ffprobe).

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | Reachability, model residency and leases |
| `POST` | `/api/engine/unload` | Free an idle GPU worker |
| `GET` | `/api/voices` | Uploaded reference voices |
| `POST` | `/api/voices/upload` | Multipart `file` + exact `transcript` |
| `POST` | `/api/voices/sample` | `mode=clone`, `voice_id`, optional text, temperature, seed |
| `POST` | `/api/books` | Upload EPUB and get chapters |
| `POST` | `/api/jobs` | Book, chapters, `voice_mode=clone`, voice ID, temperature, seed |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | Status and progress |
| `POST` | `/api/jobs/{id}/cancel`, `/resume` | Cancel/resume an EditX run |
| `GET` | `/api/jobs/{id}/download` | Finished `.m4b` |

## License

MIT for this application; see [LICENSE](LICENSE). Upstream code and model
weights retain their respective licenses.
