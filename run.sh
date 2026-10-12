#!/usr/bin/env bash
# Linux/WSL launcher. The GPU model runs in the separate EditX container.
set -euo pipefail
cd "$(dirname "$0")"
HOST="${AUDIOBOOK_HOST:-127.0.0.1}"
PORT="${AUDIOBOOK_PORT:-8005}"
export STEP_AUDIO_URL="${STEP_AUDIO_URL:-http://localhost:8006}"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required to assemble audiobooks. Install it and run again."
  exit 1
fi

tts_answers() {
  .venv/bin/python - <<'PY'
import json, os, urllib.request
try:
    with urllib.request.urlopen(os.environ['STEP_AUDIO_URL'].rstrip('/') + '/api/model-info', timeout=5) as r:
        assert json.load(r)['engine'] == 'step-audio-editx'
except Exception:
    raise SystemExit(1)
PY
}

if ! tts_answers; then
  install="${AUDIOBOOK_INSTALL_TTS:-auto}"
  # Only the default local URL is managed by the bundled compose file.
  case "$STEP_AUDIO_URL" in
    http://localhost:8006|http://127.0.0.1:8006)
      if [ "$install" = auto ] && [ -t 0 ]; then
        read -r -p "Build/start Step-Audio-EditX with Docker (large download)? [y/N] " answer
        case "$answer" in y|Y|yes|YES) install=1 ;; esac
      fi
      if [ "$install" = 1 ]; then
        docker compose up -d --build step-audio-editx
      fi
      ;;
  esac
  if ! tts_answers; then
    echo "Step-Audio-EditX is not ready at $STEP_AUDIO_URL."
    echo "Run 'docker compose up -d --build step-audio-editx' or see README.md."
    echo "The UI will remain available while the engine starts."
  fi
fi

echo "auto-audiobook -> http://$HOST:$PORT"
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
