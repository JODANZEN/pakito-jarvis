#!/bin/bash
# Get the latest Pakito from GitHub and install anything new. Your settings (config.local.json) stay as they are.
cd "$(dirname "$0")" || exit 1
echo "Getting the latest Pakito..."
if ! git pull --ff-only; then
  echo; echo "Couldn't update. Either there's no internet, or files here were changed by hand (git status shows which)."
  read -r -p "Press Enter to close."; exit 1
fi
PY=.venv/bin/python
if [ ! -x "$PY" ]; then echo "Run setup-pakito.command first."; read -r -p "Press Enter to close."; exit 1; fi
echo "Installing anything new..."
"$PY" -m pip install --disable-pip-version-check -q -r assistant/requirements.txt
"$PY" -m assistant.check_live all
echo; echo "Updated. If Pakito is running, close it and start it again."
read -r -p "Press Enter to close."
