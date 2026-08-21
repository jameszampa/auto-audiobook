#!/usr/bin/env bash
# Start the auto-audiobook web app.
#
#   ./run.sh              -> http://127.0.0.1:8005
#   AUDIOBOOK_PORT=9000 ./run.sh
#
# Narration needs a chatterbox-tts server (default localhost:8004). If nothing
# answers there, this script can clone it and bring it up in Docker; see the
# AUDIOBOOK_INSTALL_TTS / CHATTERBOX_* variables below.
set -euo pipefail

cd "$(dirname "$0")"

CHATTERBOX_URL="${CHATTERBOX_URL:-http://localhost:8004}"
HOST="${AUDIOBOOK_HOST:-127.0.0.1}"
PORT="${AUDIOBOOK_PORT:-8005}"

# auto = offer the install when stdin is a terminal, stay out of the way
# otherwise, so a service unit wrapping this script never blocks on a prompt.
# 1 = install without asking, 0 = never touch Docker.
INSTALL_TTS="${AUDIOBOOK_INSTALL_TTS:-auto}"
CHATTERBOX_REPO="${CHATTERBOX_REPO:-https://github.com/devnen/Chatterbox-TTS-Server.git}"
CHATTERBOX_DIR="${CHATTERBOX_DIR:-../chatterbox-tts-server}"
# First start builds a CUDA image and downloads model weights, which is slow
# on a cold cache but only happens once.
CHATTERBOX_WAIT_SEC="${CHATTERBOX_WAIT_SEC:-1200}"

# Our compose overlay, written into the TTS checkout. Doubles as the marker
# that says "this install is ours, so restarting it is safe", and records
# which upstream compose file it was paired with.
OVERLAY=compose.auto-audiobook.yml

tts_answers() {
  curl -fsS --max-time 5 "$CHATTERBOX_URL/api/model-info" >/dev/null 2>&1
}

# Only ever install for a server on this machine; a remote CHATTERBOX_URL is
# somebody else's to run.
url_is_local() {
  local h="${CHATTERBOX_URL#*://}"
  h="${h%%/*}"; h="${h%%:*}"
  [ "$h" = "localhost" ] || [ "$h" = "127.0.0.1" ]
}

# The port half of CHATTERBOX_URL, which is the port the container must
# publish for the app to find it.
tts_port() {
  local h="${CHATTERBOX_URL#*://}"
  h="${h%%/*}"
  case "$h" in *:*) echo "${h##*:}";; *) echo 8004;; esac
}

have_nvidia() {
  command -v nvidia-smi >/dev/null 2>&1 && return 0
  # WSL2 and CDI setups often have no nvidia-smi on PATH but do have the
  # container toolkit, which is what Docker actually needs.
  command -v nvidia-ctk >/dev/null 2>&1 || command -v nvidia-container-runtime >/dev/null 2>&1
}

# Which upstream compose file to use. The variants differ by CUDA version and
# the mapping is upstream's, from its README: cu121 (the default file) for
# RTX 20/30/40, cu128 for Blackwell/sm_120, cu130 for DGX Spark/sm_121.
pick_compose_file() {
  if [ -n "${CHATTERBOX_COMPOSE_FILE:-}" ]; then
    echo "$CHATTERBOX_COMPOSE_FILE"; return 0
  fi
  local cap=""
  if command -v nvidia-smi >/dev/null 2>&1; then
    cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
  fi
  case "$cap" in
    12.0)      echo docker-compose-cu128.yml; return 0;;
    12.1|13.*) echo docker-compose-cu130.yml; return 0;;
  esac
  if have_nvidia; then
    # No compute capability to go on (nvidia-smi absent, as under WSL), so
    # take the mainstream CUDA 12.1 build.
    echo docker-compose.yml; return 0
  fi
  return 1
}

# Two corrections to whichever upstream compose file we picked, kept in one
# overlay so the upstream checkout stays untouched and updatable.
write_overlay() {
  local dir="$1" base="$2" port="$3"
  if [ -e "$dir/$OVERLAY" ] && ! grep -q '^# written by auto-audiobook' "$dir/$OVERLAY"; then
    echo "  $dir/$OVERLAY exists and is not ours — leaving it alone."
    return 1
  fi
  cat > "$dir/$OVERLAY" <<YAML
# written by auto-audiobook's run.sh — safe to delete, it will be recreated.
# base: $base
#
# 1. Upstream's compose files hardcode HF_TOKEN=YOUR_TOKEN_HERE. No server code
#    reads it; huggingface_hub does, and would send the placeholder as a bearer
#    token, so model downloads fail with a 401 that looks like a network fault.
# 2. The port is pinned to the one in CHATTERBOX_URL. Some upstream files
#    publish \${PORT:-8004}, so a PORT left over in the environment would move
#    the server somewhere this app is not looking. !override replaces the
#    upstream list instead of appending to it (needs compose 2.24.4+).
services:
  chatterbox-tts-server:
    environment:
      HF_TOKEN: ""
    ports: !override
      - "$port:8004"
YAML
}

compose_up() {
  local dir="$1" base="$2"
  # Docker creates missing bind-mount sources as root, which then breaks
  # voice uploads from this app. Make them ourselves first.
  ( cd "$dir" && mkdir -p model_cache outputs logs voices reference_audio )
  mkdir -p "$HOME/.cache/huggingface"
  # cu130 declares env_file: .env and compose refuses to start without it.
  if grep -q 'env_file: .env' "$dir/$base" && [ ! -e "$dir/.env" ]; then
    : > "$dir/.env"
  fi
  if ! ( cd "$dir" && docker compose -f "$base" -f "$OVERLAY" config -q ); then
    echo "  that pair of compose files does not validate — check the output above."
    echo "  (the ports: !override in $OVERLAY needs docker compose 2.24.4 or newer)"
    return 1
  fi
  echo "  docker compose -f $base -f $OVERLAY up -d   (in $dir)"
  ( cd "$dir" && docker compose -f "$base" -f "$OVERLAY" up -d )
}

wait_for_tts() {
  local dir="$1" waited=0
  echo "  waiting for $CHATTERBOX_URL (up to ${CHATTERBOX_WAIT_SEC}s; the first"
  echo "  start downloads model weights, ~5 minutes on a fast link) ..."
  while [ "$waited" -lt "$CHATTERBOX_WAIT_SEC" ]; do
    if tts_answers; then
      echo "  chatterbox-tts is up after ${waited}s."
      return 0
    fi
    sleep 10
    waited=$((waited + 10))
    if [ $((waited % 60)) -eq 0 ]; then
      echo "  ... still starting (${waited}s)"
    fi
  done
  echo "  gave up after ${waited}s. Last lines of its log:"
  ( cd "$dir" && docker compose logs --tail 20 chatterbox-tts-server 2>&1 | sed 's/^/    /' ) || true
  return 1
}

# An existing checkout, ours or the user's. First match wins so we never
# clone a second copy next to one that is already there.
find_checkout() {
  local c
  for c in "$CHATTERBOX_DIR" ../Chatterbox-TTS-Server ../chatterbox \
           "$HOME/Chatterbox-TTS-Server" "$HOME/chatterbox-tts-server"; do
    if [ -f "$c/server.py" ] && [ -f "$c/docker-compose.yml" ]; then
      ( cd "$c" && pwd )
      return 0
    fi
  done
  return 1
}

confirm() {
  case "$INSTALL_TTS" in
    1|yes|true) return 0;;
    0|no|false) return 1;;
  esac
  [ -t 0 ] || return 1
  local reply
  read -r -p "$1 [Y/n] " reply
  case "$reply" in ""|y|Y|yes|YES) return 0;; *) return 1;; esac
}

install_chatterbox() {
  local dir base
  case "$INSTALL_TTS" in 0|no|false) return 1;; esac
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "  Docker is not available (not installed, daemon down, or this user"
    echo "  is not in the docker group), so the TTS server cannot be set up here."
    return 1
  fi

  dir="$(find_checkout || true)"

  # Ours and merely stopped: bring it back without asking.
  if [ -n "$dir" ] && [ -f "$dir/$OVERLAY" ]; then
    base=$(sed -n 's/^# base: //p' "$dir/$OVERLAY" | head -1)
    [ -n "$base" ] && [ -f "$dir/$base" ] || base=docker-compose.yml
    echo "  restarting the chatterbox-tts install at $dir"
    # Rewrite the overlay too, so a CHATTERBOX_URL port changed since the
    # install still lands where this app looks.
    write_overlay "$dir" "$base" "$(tts_port)" || return 1
    compose_up "$dir" "$base" && wait_for_tts "$dir"
    return $?
  fi

  # Someone else's checkout: say how to start it, but do not guess which
  # compose file they built it from and risk a second, conflicting stack.
  if [ -n "$dir" ]; then
    echo "  found a chatterbox-tts checkout at $dir, but nothing is answering."
    echo "  start it with the compose file you built it from, e.g."
    echo "    (cd $dir && docker compose -f docker-compose.yml up -d)"
    return 1
  fi

  if ! base="$(pick_compose_file)"; then
    echo "  no NVIDIA GPU support detected. Chatterbox runs on CPU, but a book"
    echo "  would take days rather than hours, so this is not done for you."
    echo "  To install anyway:  CHATTERBOX_COMPOSE_FILE=docker-compose-cpu.yml ./run.sh"
    if [ -e /dev/kfd ]; then
      echo "  For an AMD GPU, pick the ROCm compose file upstream recommends for it."
    fi
    return 1
  fi

  confirm "Install chatterbox-tts into $CHATTERBOX_DIR with Docker ($base)?
  It clones the repo, builds a multi-GB CUDA image and downloads model
  weights on first start — expect 10-30 minutes." || {
    echo "  skipped."
    return 1
  }

  if ! command -v git >/dev/null 2>&1; then
    echo "  git is needed to fetch $CHATTERBOX_REPO — install it and re-run."
    return 1
  fi
  echo "  cloning $CHATTERBOX_REPO -> $CHATTERBOX_DIR"
  if ! git clone --depth 1 "$CHATTERBOX_REPO" "$CHATTERBOX_DIR"; then
    echo "  clone failed — nothing was installed."
    return 1
  fi
  dir="$( cd "$CHATTERBOX_DIR" && pwd )"
  if [ ! -f "$dir/$base" ]; then
    echo "  $base is not in that repo — set CHATTERBOX_COMPOSE_FILE to one of:"
    ( cd "$dir" && printf '    %s\n' docker-compose*.yml )
    return 1
  fi

  write_overlay "$dir" "$base" "$(tts_port)" || return 1
  compose_up "$dir" "$base" && wait_for_tts "$dir"
}

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv ..."
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip -q
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

# Chapter stitching and the .m4b itself are ffmpeg's job; a job would run for
# hours and only then fail at assembly without it.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "WARNING: ffmpeg is not on PATH. Narration will run, but assembling the"
  echo "  .m4b at the end will fail. Install it (apt install ffmpeg / brew install ffmpeg)."
fi

if ! tts_answers; then
  echo "chatterbox-tts is not answering at $CHATTERBOX_URL"
  if url_is_local; then
    install_chatterbox || true
  else
    echo "  CHATTERBOX_URL points at another machine, so nothing is installed here."
  fi
fi

if ! tts_answers; then
  echo "WARNING: still nothing at $CHATTERBOX_URL — the UI will load and show"
  echo "  the engine as down. See https://github.com/devnen/Chatterbox-TTS-Server"
  echo "  for the manual setup, and set CHATTERBOX_URL if it is not on :8004."
fi

echo "auto-audiobook -> http://$HOST:$PORT"
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
