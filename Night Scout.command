#!/bin/zsh
# Night Scout for macOS. Double-click to start it; it opens in your browser (the Setup
# walkthrough the first time, your Digest after that). Close this window to stop it.
# The first launch installs Night Scout into .venv here, which takes a few minutes.
cd "$(dirname "$0")" || exit 1
pause() { read -k1 "?Press any key to close this window."; }

if [ ! -x .venv/bin/night-scout ]; then
  py=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      py="$c"; break
    fi
  done
  if [ -z "$py" ]; then
    echo "Night Scout needs Python 3.11 or newer: https://www.python.org/downloads/"
    echo "(or with Homebrew: brew install python@3.12), then double-click this again."
    pause; exit 1
  fi
  echo "First launch: installing Night Scout into .venv with $("$py" --version)…"
  if ! { "$py" -m venv .venv && .venv/bin/python -m pip install -q --upgrade pip \
         && .venv/bin/python -m pip install -q -e .; }; then
    echo "The install didn't finish (see above)."; rm -rf .venv; pause; exit 1
  fi
fi

# Ollama runs Gemma on this Mac. Start it if it's installed but not running.
if ! curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; then
  if [ -d /Applications/Ollama.app ]; then
    echo "Starting Ollama…"; open -ga Ollama
  elif command -v ollama >/dev/null 2>&1; then
    echo "Starting Ollama…"; nohup ollama serve >/dev/null 2>&1 &
  else
    echo "Ollama isn't installed (https://ollama.com). You can also pick a cloud model in Setup."
  fi
  for i in {1..20}; do curl -s --max-time 1 http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
fi

config=$(.venv/bin/python -c 'from night_scout.core.paths import default_config_path as p; print(p())')
if [ -f "$config" ]; then
  .venv/bin/night-scout serve "$@"
else
  .venv/bin/night-scout setup "$@"   # first time: the Setup walkthrough
fi
rc=$?
[ $rc -eq 0 ] || [ $rc -eq 130 ] || { echo "Night Scout stopped with an error (exit $rc)."; pause; }
exit $rc
