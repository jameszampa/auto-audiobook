# auto-audiobook

Drop in an `.epub`, pick a cloned voice, click **Run** — get back a chaptered
`.m4b` audiobook narrated in that voice.

The narration is done by a local [chatterbox-tts](https://github.com/devnen/Chatterbox-TTS-Server)
server, so nothing leaves the machine and there is no per-character API cost.

```
 browser  ──uploads .epub──▶  auto-audiobook (FastAPI, :8005)
                                   │  chapters ─▶ sentence-aware chunks
                                   │  POST /tts per chunk
                                   ▼
                             chatterbox-tts (:8004, GPU)
                                   │  wav per chunk
                                   ▼
                             ffmpeg ─▶ chaptered .m4b
```

## Requirements

- A running **chatterbox-tts** server (default `http://localhost:8004`)
- **Python 3.10+** and **ffmpeg** on `PATH`

## Quick start

```bash
./run.sh                       # first run creates .venv and installs deps
```

Then open **http://127.0.0.1:8005**.

The header shows engine status. If it reads *engine down*, start the TTS server
(see the [Chatterbox-TTS-Server](https://github.com/devnen/Chatterbox-TTS-Server)
README for the Docker setup) and set `CHATTERBOX_URL` if it is not on
`http://localhost:8004`.

*engine idle — model unloaded* is normal: see below.

## The GPU is only busy while you are making something

The TTS container stays up, but its model does not stay in VRAM. This app
loads the model when a job (or an uncached voice preview) actually needs it and
unloads it again once nothing has needed it for `AUDIOBOOK_ENGINE_IDLE_SEC`
(default 180s) — including at startup and shutdown, since an app with no job
running is by definition idle. Measured on an RTX 4090 deployment: unloading
frees ~3.9 GB and takes ~3s, and loading back takes ~11s, which is noise next
to the hours a book takes.

Consequences worth knowing:

- The first chunk of a job waits ~11s for the model. Nothing else does.
- **Resume never loads the model if every chunk is already on disk** — the
  lease is taken on the first chunk that genuinely has to be synthesized, and
  assembly (ffmpeg) runs with the GPU already released.
- If you also drive chatterbox from its own UI on `:8004`, this app will pull
  the model out from under it once it goes idle. Set
  `AUDIOBOOK_MANAGE_ENGINE=0` to leave the server's model alone entirely; the
  app then expects a model to already be loaded, as before.
- `POST /api/engine/unload` frees the GPU immediately instead of waiting out
  the timer. It refuses while a job or preview holds the model.

## Using it

1. **Drop your book.** The epub is parsed into chapters in reading order.
   Copyright/contents/acknowledgement pages and very short sections are
   unticked automatically — tick anything you do want narrated.
2. **Pick a voice.** *Cloned voices* are reference clips on the TTS server;
   press ▶ on any of them to hear a preview before committing hours of GPU
   time. Use **Upload a voice clone .wav** to add your own — 10–30 seconds of
   clean speech works well.
3. **Run.** Progress, ETA and a live log appear under *Audiobooks*. When it
   finishes, download the `.m4b`.

Estimates shown in the UI come from measured throughput on an RTX 4090
deployment (~14.5 characters of text per second of speech, ~3.8× faster than
real time), so a 9-hour book takes roughly 2.4 hours to generate.

## Why it is built this way

- **Chunking happens here, not in the TTS server.** Each chunk is an
  independently retryable, resumable unit, which is what makes progress
  reporting and resume-after-crash possible on a multi-hour job.
- **Every chunk is written to disk as it is produced.** Killing the app, or
  the machine losing power, costs at most one chunk. Press **Resume** and it
  reuses everything already on disk.
- **One job at a time.** A single worker serialises GPU access; running jobs in
  parallel on one GPU makes them all slower, not faster.
- **The seed is fixed by default (12345).** Seed `0` means "random" to the
  engine, and a random seed per chunk makes the narrator's timbre drift audibly
  across a book. Leave it fixed unless you are deliberately auditioning voices.
- **Sentence-aware splitting.** Chunks break on sentence boundaries, with an
  abbreviation list so `Mr. Havelock` is not read as two sentences.

## Layout

```
app/
  main.py        FastAPI routes
  jobs.py        job queue, persistence, resume, cancel
  engine.py      loads the model on demand, unloads it when idle
  epub_parse.py  epub -> ordered chapters
  chunker.py     chapters -> sentence-aware chunks
  tts_client.py  chatterbox client (retries, voice params)
  audio.py       ffmpeg stitching and .m4b assembly
static/          the single-page UI
tests/           unit tests + test epub generator
data/            uploads, per-job chunks, finished audiobooks (gitignored)
```

## Configuration

All optional, set as environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CHATTERBOX_URL` | `http://localhost:8004` | TTS server address |
| `AUDIOBOOK_TTS_TIMEOUT` | `600` | seconds to wait for a single TTS request |
| `AUDIOBOOK_TTS_RETRIES` | `3` | attempts per chunk before a job fails |
| `AUDIOBOOK_MANAGE_ENGINE` | `1` | load/unload the model on demand (`0` = never touch it) |
| `AUDIOBOOK_ENGINE_IDLE_SEC` | `180` | idle seconds before the model is unloaded |
| `AUDIOBOOK_ENGINE_LOAD_TIMEOUT` | `600` | patience for a model load (first ever load downloads weights) |
| `AUDIOBOOK_PORT` | `8005` | port for this app |
| `AUDIOBOOK_HOST` | `127.0.0.1` | bind address (`0.0.0.0` to expose on LAN) |
| `AUDIOBOOK_CHUNK_CHARS` | `240` | target characters per TTS request |
| `AUDIOBOOK_CHAPTER_GAP_MS` | `1100` | silence between chapters |
| `AUDIOBOOK_CHUNK_GAP_MS` | `220` | silence between chunks |
| `AUDIOBOOK_MIN_CHAPTER_CHARS` | `300` | shorter sections start unticked |
| `AUDIOBOOK_M4B_BITRATE` | `64k` | AAC bitrate |
| `AUDIOBOOK_DATA_DIR` | `./data` | where books and output live |

## Tests

```bash
./.venv/bin/python tests/test_units.py       # chunker + epub parsing, no GPU needed
./.venv/bin/python tests/make_test_epub.py   # writes tests/sample-book.epub
```

## API

The UI is a client of a plain REST API, usable directly:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | engine reachability, whether a model is resident, who is using it |
| `POST` | `/api/engine/unload` | free the GPU now |
| `GET` | `/api/voices` | clone + built-in voices |
| `POST` | `/api/voices/upload` | add a voice-clone wav |
| `POST` | `/api/voices/sample` | short preview wav |
| `POST` | `/api/books` | upload epub, get chapters |
| `POST` | `/api/jobs` | start a run |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | status and progress |
| `POST` | `/api/jobs/{id}/cancel`, `/resume` | control a run |
| `GET` | `/api/jobs/{id}/download` | finished `.m4b` |

## License

MIT — see [LICENSE](LICENSE).
