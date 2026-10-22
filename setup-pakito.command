#!/bin/bash
# Pakito setup for Mac: double-click it in Finder (or run: bash setup-pakito.command). Safe to run again.
cd "$(dirname "$0")" || exit 1
echo "Pakito setup (Mac)"

# 1) a Python the mic library supports (3.10-3.13)
pick_python() {
  for v in 3.13 3.12 3.11 3.10; do
    for p in "/opt/homebrew/bin/python$v" "/usr/local/bin/python$v" \
             "/Library/Frameworks/Python.framework/Versions/$v/bin/python3" "$(command -v python$v 2>/dev/null)"; do
      [ -n "$p" ] && [ -x "$p" ] && { echo "$p"; return; }
    done
  done
  p="$(command -v python3 2>/dev/null)"
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
    echo "$p"
  fi
}
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
  echo "The .venv folder was made with a Python Pakito can't use. Making a new one..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  PY="$(pick_python)"
  if [ -z "$PY" ]; then
    echo
    echo "Pakito needs Python 3.10 to 3.13 (its microphone library doesn't support newer ones yet)."
    if command -v brew >/dev/null; then
      echo "Easiest:  brew install python@3.13   then run this again."
    else
      echo "Install Python 3.13 from python.org (macOS 64-bit universal installer), then run this again."
      open "https://www.python.org/downloads/macos/"
    fi
    read -r -p "Press Enter to close."; exit 1
  fi
  echo "Creating a private Python environment in .venv with $PY ..."
  "$PY" -m venv .venv || { read -r -p "Press Enter to close."; exit 1; }
fi
PY=.venv/bin/python
echo "Using Python: $PY ($($PY -c 'import sys; print(sys.version.split()[0])'))"

# 2) the mic library needs PortAudio on a Mac
if ! "$PY" -c 'import pyaudio' 2>/dev/null; then
  if command -v brew >/dev/null; then
    brew list portaudio >/dev/null 2>&1 || { echo "Installing PortAudio (for the mic)..."; brew install portaudio; }
    export CFLAGS="-I$(brew --prefix)/include" LDFLAGS="-L$(brew --prefix)/lib"
  else
    echo "The mic library needs PortAudio. Install Homebrew (https://brew.sh), then: brew install portaudio"
    echo "and run this again."
    read -r -p "Press Enter to close."; exit 1
  fi
fi

echo "Installing packages (a minute or two the first time)..."
"$PY" -m pip install --disable-pip-version-check -q -r assistant/requirements.txt || { read -r -p "Press Enter to close."; exit 1; }
echo
"$PY" -m assistant.setup_wizard
echo
echo "All set. Start Pakito with run-pakito.command (double-click, or ./run-pakito.command in Terminal)."
read -r -p "Press Enter to close."
