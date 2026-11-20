#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
  echo "Missing .env file. Create it and set TELEGRAM_BOT_TOKEN first." >&2
  exit 1
fi

# Read local configuration without printing it (the file contains a bot token).
set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "TELEGRAM_BOT_TOKEN is empty in .env." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi

VENV_PYTHON="venv/bin/python"
if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* || "$(uname -s)" == CYGWIN* ]]; then
  VENV_PYTHON="venv/Scripts/python.exe"
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  "$PYTHON" -m venv venv
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Could not create the Python virtual environment at venv/. Check your Python installation." >&2
  exit 1
fi

mkdir -p data
export LOCAL_MODE=1
export DB_PATH="${DB_PATH:-$(pwd)/data/users.db}"

"$VENV_PYTHON" -m pip install -q -r requirements.txt
echo "Starting local bot and Flask server on http://127.0.0.1:${PORT:-8080}"
exec "$VENV_PYTHON" bot.py
