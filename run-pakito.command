#!/bin/bash
# Start Pakito on a Mac: double-click it, or ./run-pakito.command [setup|text|spotify|calendar|check ...|keys]
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
if [ ! -x "$PY" ]; then echo "Run setup-pakito.command first."; read -r -p "Press Enter to close."; exit 1; fi
case "$1" in
  setup)    "$PY" -m assistant.setup_wizard ;;
  text)     "$PY" -m assistant ;;
  spotify)  "$PY" -m assistant.spotify ;;
  youtube)  "$PY" -m assistant.podcasts ;;
  calendar) "$PY" -m assistant.calendar_feed ;;
  mcp)      "$PY" assistant/mcp_server.py register ;;
  keys)     "$PY" -m assistant.voice keytest ;;
  check)    shift; "$PY" -m assistant.check_live "$@" ;;
  *)        "$PY" -m assistant.voice ;;
esac
