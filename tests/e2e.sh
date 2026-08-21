#!/usr/bin/env bash
# End-to-end test: epub in, chaptered .m4b out, through the real TTS engine.
#   ./tests/e2e.sh
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8005}"
HERE="$(cd "$(dirname "$0")" && pwd)"
EPUB="$HERE/sample-book.epub"

pass=0; fail=0
ok()  { echo "  PASS: $*"; pass=$((pass+1)); }
bad() { echo "  FAIL: $*"; fail=$((fail+1)); }

echo "== engine + app =="
status=$(curl -fsS --max-time 10 "$BASE/api/status") || { bad "app unreachable"; exit 1; }
echo "    $status"
[ "$(echo "$status" | jq -r .ready)" = "true" ] && ok "engine ready" || { bad "engine not ready"; exit 1; }

echo "== voices =="
voices=$(curl -fsS "$BASE/api/voices")
clone_n=$(echo "$voices" | jq '.clone | length')
VOICE=$(echo "$voices" | jq -r '.clone[0].id')
[ "$clone_n" -gt 0 ] && ok "$clone_n cloning voices (using '$VOICE')" || { bad "no clone voices"; exit 1; }

echo "== upload epub =="
[ -f "$EPUB" ] || "$HERE/../.venv/bin/python" "$HERE/make_test_epub.py" "$EPUB" >/dev/null
book=$(curl -fsS -X POST "$BASE/api/books" -F "file=@$EPUB")
BOOK_ID=$(echo "$book" | jq -r .book_id)
title=$(echo "$book" | jq -r .title)
total=$(echo "$book" | jq '.chapters | length')
included=$(echo "$book" | jq '[.chapters[] | select(.include)] | length')
echo "    '$title' — $total sections, $included ticked"
[ "$BOOK_ID" != "null" ] && ok "epub parsed (book_id set)" || bad "no book_id"
[ "$total" = "5" ] && ok "all 5 sections detected" || bad "expected 5 sections, got $total"
[ "$included" = "3" ] && ok "front matter auto-excluded, 3 chapters ticked" \
  || bad "expected 3 ticked, got $included"

echo "== start job =="
sel=$(echo "$book" | jq -c '[.chapters[] | select(.include) | .index]')
job=$(curl -fsS -X POST "$BASE/api/jobs" \
  -F "book_id=$BOOK_ID" -F "voice_mode=clone" -F "voice_id=$VOICE" \
  -F "chapters=$sel")
JOB_ID=$(echo "$job" | jq -r .id)
chunks=$(echo "$job" | jq -r .progress.chunks_total)
[ "$JOB_ID" != "null" ] && ok "job queued ($chunks chunks)" || { bad "job not created"; exit 1; }

echo "== wait for completion =="
for i in $(seq 1 240); do
  j=$(curl -fsS "$BASE/api/jobs/$JOB_ID")
  st=$(echo "$j" | jq -r .status)
  done_n=$(echo "$j" | jq -r .progress.chunks_done)
  case "$st" in
    completed) ok "job completed ($done_n/$chunks chunks)"; break;;
    failed|cancelled) bad "job $st: $(echo "$j" | jq -r .error)"; break;;
  esac
  [ $((i % 10)) -eq 0 ] && echo "    ...$st $done_n/$chunks"
  sleep 5
done
[ "$st" = "completed" ] || { echo "$j" | jq -r '.log[]' | tail -15; echo "==== $pass passed, $fail failed ===="; exit 1; }

echo "== verify output =="
out=$(curl -fsS "$BASE/api/jobs/$JOB_ID" | jq -c .output)
echo "    $out" | head -c 400; echo
FILE=$(echo "$out" | jq -r .path)
[ -f "$FILE" ] && ok "m4b exists on disk" || bad "m4b missing at $FILE"

# Chapter markers are the thing that makes it an audiobook rather than a big mp3.
nch=$(ffprobe -v error -print_format json -show_chapters "$FILE" | jq '.chapters | length')
[ "$nch" = "3" ] && ok "m4b has 3 chapter markers" || bad "expected 3 chapters, got $nch"

dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FILE")
python3 -c "import sys; d=float('$dur'); sys.exit(0 if d>20 else 1)" \
  && ok "duration ${dur}s is plausible" || bad "duration ${dur}s too short"

codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$FILE")
[ "$codec" = "aac" ] && ok "audio codec is aac" || bad "codec is $codec"

ffprobe -v error -print_format json -show_chapters "$FILE" \
  | jq -r '.chapters[] | "    ch: \(.tags.title)  \(.start_time)s -> \(.end_time)s"'

echo "== download endpoint =="
code=$(curl -s -o /tmp/e2e-download.m4b -w '%{http_code}' "$BASE/api/jobs/$JOB_ID/download")
[ "$code" = "200" ] && [ -s /tmp/e2e-download.m4b ] && ok "download returns the file" \
  || bad "download HTTP $code"
rm -f /tmp/e2e-download.m4b

echo "== resume reuses existing chunks =="
curl -fsS -X POST "$BASE/api/jobs/$JOB_ID/resume" >/dev/null
for i in $(seq 1 60); do
  st=$(curl -fsS "$BASE/api/jobs/$JOB_ID" | jq -r .status)
  [ "$st" = "completed" ] && break
  [ "$st" = "failed" ] && break
  sleep 2
done
reused=$(curl -fsS "$BASE/api/jobs/$JOB_ID" | jq -r '.log[] | select(contains("Reused"))' | tail -1)
[ -n "$reused" ] && ok "resume reused cached chunks — $reused" || bad "resume did not report reuse"

echo
echo "==== $pass passed, $fail failed ===="
[ "$fail" -eq 0 ]
