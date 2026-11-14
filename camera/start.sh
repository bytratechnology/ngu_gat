#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -f .venv/.installed ]; then
  PY=""
  for c in python3.10 python3.11 python3.12 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)'; then
      PY="$c"
      break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Can Python 3.9 - 3.12 (khuyen dung 3.10): https://www.python.org/downloads/"
    exit 1
  fi
  echo "Cai dat lan dau bang $PY ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.installed
fi

if [ ! -f models/AgeModel/model.safetensors ] && [ -f download_models.py ]; then
  .venv/bin/python download_models.py
fi

exec .venv/bin/python drowsy.py --port auto "$@"
