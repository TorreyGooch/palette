#!/usr/bin/env bash
# quotesource CLI wrapper (Linux/macOS). Windows: use qs.bat
cd "$(dirname "$0")" || exit 1
PYTHONPATH="$(pwd):${PYTHONPATH}" exec "${QS_PYTHON:-python3}" -m quotesource "$@"
