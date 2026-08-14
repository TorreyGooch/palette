#!/usr/bin/env bash
# quotesource CLI wrapper (Linux/macOS). Windows: use qs.bat
#
# Finding the interpreter matters more than it looks. The system python3 can
# import quotesource fine, so commands appear to work right up until one
# needs faster-whisper or onnxruntime-gpu and reports them "not installed" -
# when they are installed, in the environment this did not pick. Prefer an
# environment that actually has them, and let QS_PYTHON override.
cd "$(dirname "$0")" || exit 1

pick_python() {
  if [ -n "${QS_PYTHON:-}" ]; then echo "$QS_PYTHON"; return; fi
  for candidate in \
    "$HOME/miniconda3/envs/palette/bin/python" \
    "$PWD/.venv/bin/python" \
    "$HOME/.venv/palette/bin/python"
  do
    if [ -x "$candidate" ] && "$candidate" -c "import faster_whisper" 2>/dev/null; then
      echo "$candidate"; return
    fi
  done
  echo "python3"
}

PY="$(pick_python)"
PYTHONPATH="$(pwd):${PYTHONPATH}" exec "$PY" -m quotesource "$@"
