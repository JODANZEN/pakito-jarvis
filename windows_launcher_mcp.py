#!/usr/bin/env python3
"""
windows_launcher MCP server — minimal app/URL launcher for Jarvis on Windows.

Deliberately launch-only: no window control, no clicking, no keystrokes.
(That is a later phase.) Two tools:

  open_app(path_or_command)  - launch an application or open a file/folder
  open_url(url)              - open a URL in the default browser

This file is meant to run ON WINDOWS when deployed. On other platforms
open_app returns a clear error instead of crashing, so the server can still
be imported/tested on Linux/macOS.

Dependencies: pip install mcp
Run: registered in Jarvis's config.json mcp_servers section (stdio transport).
"""

import os
import subprocess
import sys
import webbrowser

# See note in ask_claude_mcp.py if this import fails on your mcp version.
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("windows-launcher")


@mcp.tool()
def open_app(path_or_command: str) -> str:
    """Launch a Windows application, or open a file/folder with its default app.

    Accepts either a full path (e.g. 'C:\\Program Files\\Notepad++\\notepad++.exe',
    'C:\\Users\\me\\report.pdf') or a command name on PATH / an app alias that
    Windows 'start' understands (e.g. 'notepad', 'calc', 'code').
    Launch-only: cannot control, focus, or click inside the app afterwards.
    """
    target = os.path.expandvars(os.path.expanduser(path_or_command.strip()))
    if not target:
        return "ERROR: empty path_or_command"

    if sys.platform != "win32":
        return ("ERROR: open_app only works on Windows (current platform: %s). "
                "This tool is a no-op here." % sys.platform)

    try:
        if os.path.exists(target):
            # Files, folders, .exe, .lnk — Windows picks the right handler.
            os.startfile(target)  # noqa: S606 (intentional; launch-only tool)
        else:
            # Not a path: let cmd's `start` resolve it (PATH, app aliases,
            # 'start' built-ins). Empty "" is start's window-title slot.
            subprocess.Popen(
                ["cmd", "/c", "start", "", target],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return "Launched: %s" % target
    except OSError as e:
        return "ERROR: failed to launch %r: %s" % (target, e)


@mcp.tool()
def open_url(url: str) -> str:
    """Open a URL in the user's default web browser."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return "ERROR: only http:// and https:// URLs are allowed, got: %r" % url
    try:
        ok = webbrowser.open(url)
    except Exception as e:  # webbrowser can raise odd platform errors
        return "ERROR: failed to open URL: %s" % e
    if ok:
        return "Opened in default browser: %s" % url
    return "ERROR: no usable web browser found to open %s" % url


if __name__ == "__main__":
    mcp.run(transport="stdio")
