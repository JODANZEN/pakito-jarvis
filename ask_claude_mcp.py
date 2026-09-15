#!/usr/bin/env python3
"""
ask_claude MCP server — escalation path from GLM-5.2 up to Claude.

A minimal MCP stdio server exposing one tool, ask_claude(question, context),
which calls the Anthropic Messages API and returns the text response. Jarvis
launches this as a subprocess (stdio transport) via its config.json MCP
servers section.

Environment variables:
  ANTHROPIC_API_KEY  (REQUIRED at call time) Anthropic API key.
  CLAUDE_MODEL       default: claude-opus-4-8  <-- verify this model id is
                     current at https://docs.anthropic.com/en/docs/about-claude/models
  ANTHROPIC_API_URL  default: https://api.anthropic.com/v1/messages
  CLAUDE_MAX_TOKENS  default: 2048

Dependencies: pip install mcp   (official Model Context Protocol Python SDK;
the Anthropic call itself uses only stdlib urllib, no anthropic package needed)

Run standalone for a smoke test:  python ask_claude_mcp.py
(it will sit waiting on stdin — that's correct for a stdio MCP server)
"""

import json
import os
import urllib.request
from urllib.error import HTTPError, URLError

# Official MCP Python SDK. FastMCP is the high-level API: decorated functions
# become tools, type hints + docstring become the tool schema. If your
# installed `mcp` version moved FastMCP, try: from fastmcp import FastMCP
# (the standalone fastmcp package) — see README "NOT YET VERIFIED".
from mcp.server.fastmcp import FastMCP

ANTHROPIC_API_URL = os.environ.get(
    "ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages"
)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "2048"))

mcp = FastMCP("ask-claude")


@mcp.tool()
def ask_claude(question: str, context: str = "") -> str:
    """Escalate a hard question to Claude (a much more capable model).

    Use this when the question requires deep reasoning, long/complex code,
    or when you are not confident in your own answer. Pass any relevant
    conversation context in the `context` argument, since Claude cannot see
    this conversation.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return ("ERROR: ANTHROPIC_API_KEY is not set in this MCP server's "
                "environment. Add it to the mcp_servers entry in Jarvis's "
                "config.json.")

    if context.strip():
        user_content = "Context:\n%s\n\nQuestion:\n%s" % (context, question)
    else:
        user_content = question

    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "messages": [{"role": "user", "content": user_content}],
    }
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        try:
            data = json.loads(resp.read())
        finally:
            resp.close()
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:1000]
        except Exception:
            pass
        return "ERROR: Anthropic API returned HTTP %s: %s" % (e.code, detail)
    except URLError as e:
        return "ERROR: could not reach Anthropic API: %s" % e.reason

    # Response shape: {"content": [{"type": "text", "text": "..."}], ...}
    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(p for p in parts if p).strip()
    return text or "(Claude returned an empty response)"


if __name__ == "__main__":
    # stdio is FastMCP's default transport; being explicit costs nothing.
    mcp.run(transport="stdio")
