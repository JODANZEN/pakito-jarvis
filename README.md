# pakito-jarvis

Drop-in infrastructure that lets [isair/jarvis](https://github.com/isair/jarvis) — a
local voice assistant that speaks the Ollama HTTP API and nothing else — run against any
OpenAI-compatible model provider, and call custom tools over MCP. The problem it solves:
Jarvis hardcodes the Ollama protocol, so swapping its "brain" for a hosted model normally
means forking it; this repo makes that a proxy configuration change instead of a patch.

Two pieces, independently useful:

- **`ollama_glm_proxy.py`** — a translation proxy that accepts Ollama-shaped requests and
  forwards them to an OpenAI-compatible upstream (Groq, Z.ai, or anything else that speaks
  that dialect), converting responses back into the shapes Jarvis expects.
- **Two MCP servers** — `ask_claude` (escalate a hard question to the Anthropic API) and
  `windows_launcher` (open apps and URLs from voice).

Python 3.9+. The proxy and its test suite are standard library only; the MCP servers need
`mcp>=1.0`.

## Run it

```bash
pip install -r requirements.txt          # only needed for the MCP servers
```

Start the proxy, pointed at Groq's free tier:

```bash
GLM_API_KEY=your-groq-key \
GLM_API_BASE=https://api.groq.com/openai/v1 \
GLM_MODEL=openai/gpt-oss-120b \
python ollama_glm_proxy.py
```

PowerShell:

```powershell
$env:GLM_API_KEY="your-groq-key"
$env:GLM_API_BASE="https://api.groq.com/openai/v1"
$env:GLM_MODEL="openai/gpt-oss-120b"
python ollama_glm_proxy.py
```

It binds `127.0.0.1:11435` and exits code 1 with a clear message if `GLM_API_KEY` is unset,
before binding the port. Smoke test:

```bash
curl http://127.0.0.1:11435/api/chat \
  -d '{"model":"openai/gpt-oss-120b","stream":false,"messages":[{"role":"user","content":"hi"}]}'
```

Then copy `config.patch.example.json`, fill in the blanks, and merge it into Jarvis's
`config.json` (`%USERPROFILE%\.config\jarvis\config.json` on Windows). Restart Jarvis fully.
See `.env.example` for every environment variable the code reads.

## Tests

```bash
python -m unittest test_proxy -v
```

13 tests, no network and no API key required — the upstream is mocked, and several tests run
the real proxy process against a fake upstream over real sockets. Coverage: non-streaming and
streaming chat (SSE → NDJSON), `/api/generate`, tool-call conversion in both directions
including streamed deltas, embeddings passthrough byte-for-byte, upstream error handling, and
the startup key check.

## How it works

Jarvis only knows how to talk to Ollama. The obvious alternative — patching Jarvis to call a
hosted API directly — means maintaining a fork against upstream changes, and re-doing it for
every provider. A proxy that impersonates Ollama keeps Jarvis completely unmodified: you point
`ollama_base_url` at port 11435 and it cannot tell the difference.

That choice buys three things:

- **Provider independence.** The upstream is three environment variables. Groq's free tier,
  Z.ai's GLM, or any OpenAI-compatible endpoint — no code changes.
- **Selective interception.** Only `/api/chat` and `/api/generate` are translated. Embeddings
  and everything else pass straight through to a real local Ollama on 11434, so
  `nomic-embed-text` keeps serving the vector store locally while chat goes to a hosted model.
  `/api/tags` responses get the remote model injected so Jarvis's model list isn't empty.
- **A place to put the hard part.** Ollama and OpenAI disagree about tool calls — OpenAI sends
  arguments as a JSON *string* and streams tool calls as indexed deltas that must be
  accumulated; Ollama wants a dict, complete. That translation, in both directions, is most of
  what the proxy does and most of what the tests cover.

Errors always come back as JSON rather than a dropped connection, so Jarvis surfaces a message
instead of hanging.

## Files

| File | What it is |
|---|---|
| `ollama_glm_proxy.py` | The proxy. Stdlib only. |
| `ask_claude_mcp.py` | MCP stdio server: `ask_claude(question, context="")` → Anthropic Messages API. |
| `windows_launcher_mcp.py` | MCP stdio server: `open_app(path_or_command)`, `open_url(url)`. Launch-only by design — no window control or clicking. `open_url` rejects non-http(s) schemes. |
| `test_proxy.py` | The 13-test suite. |
| `config.patch.example.json` | Template to merge into Jarvis's `config.json`. |
| `.env.example` | Every environment variable the code reads. |

## Status

**Verified.** All 13 tests pass. Proxy translation logic, streaming, tool-call round-tripping,
embeddings passthrough, and the startup key check are covered against a mocked and a real-socket
fake upstream. Both MCP servers register their tools with correct schemas against the real `mcp`
SDK (v1.28.1); the Anthropic call is verified with a mocked HTTP layer (URL, `x-api-key`,
`anthropic-version: 2023-06-01`, body shape, response extraction).

**Not verified against live services.** Honest confidence levels:

1. **Real upstream behavior — medium.** Request/response shapes follow the standard
   OpenAI-compatible format, but no real call has been made against Groq or Z.ai. Unverified:
   per-provider SSE framing quirks, and whether streamed tool-call deltas match the `index`-based
   format the proxy accumulates. Note the proxy deliberately ignores everything except `content`
   and `tool_calls` — if a provider emits `reasoning_content`, that text is dropped on purpose.
2. **Multi-turn tool conversations — medium-low.** The proxy synthesizes `id`/`tool_call_id` when
   Jarvis's history doesn't carry them. If an upstream rejects multi-turn tool exchanges, look at
   `_messages_to_openai` first.
3. **Windows launch behavior — untested.** `windows_launcher_mcp.py` was only ever exercised on
   Linux, where the non-Windows guard short-circuits it. The `os.startfile` / `cmd /c start` paths
   compile but have not been executed.
4. **MCP SDK drift.** Pinned in spirit to `mcp==1.28.1`
   (`from mcp.server.fastmcp import FastMCP`). A future version moving `FastMCP` breaks the import
   in both servers.
5. **Rate limits.** Groq's free tier for `openai/gpt-oss-120b` is roughly 30 req/min and ~1,000
   req/day, org-wide rather than per-key. Fine for one person; back-to-back agentic tool loops can
   hit it, which surfaces as a 429 in the proxy logs.

## License

MIT — see [LICENSE](LICENSE).
