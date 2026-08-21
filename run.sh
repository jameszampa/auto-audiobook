#!/usr/bin/env bash
# Start the auto-audiobook web app.
#
#   ./run.sh              -> http://127.0.0.1:8005
#   AUDIOBOOK_PORT=9000 ./run.sh
#
# Requires the chatterbox-tts server to be reachable (default localhost:8004).
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv ..."
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip -q
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

CHATTERBOX_URL="${CHATTERBOX_URL:-http://localhost:8004}"
HOST="${AUDIOBOOK_HOST:-127.0.0.1}"
PORT="${AUDIOBOOK_PORT:-8005}"

if ! curl -fsS --max-time 5 "$CHATTERBOX_URL/api/model-info" >/dev/null 2>&1; then
  echo "WARNING: chatterbox-tts is not answering at $CHATTERBOX_URL"
  echo "  start the TTS server first (see the Chatterbox-TTS-Server README)"
  echo "  and set CHATTERBOX_URL if it is not on http://localhost:8004"
  echo "The UI will still load and will show the engine as down."
fi

echo "auto-audiobook -> http://$HOST:$PORT"
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
