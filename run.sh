#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo 'Python 3 is required.' >&2
  exit 1
fi
if [[ ! -d .venv ]]; then
  python3 -m venv .venv || { echo 'On Raspberry Pi OS, install python3-venv first.' >&2; exit 1; }
fi
if ! .venv/bin/python -c 'import flask, mutagen, waitress' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
if [[ -z "${PORTAL_PASSWORD:-}" ]]; then
  printf 'Choose your portal password (not your Life360 password): '
  read -r -s PORTAL_PASSWORD
  printf '\n'
  if [[ -z "$PORTAL_PASSWORD" ]]; then
    echo 'Please choose a nonempty password.' >&2
    exit 1
  fi
  export PORTAL_PASSWORD
fi
if ! command -v ffplay >/dev/null; then
  echo 'Note: server-speaker playback needs ffplay: sudo apt install ffmpeg'
  echo 'Browser audio preview is available without it.'
fi
exec .venv/bin/python app.py
