#!/usr/bin/env python3
"""
Ollama -> GLM-5.2 (Z.ai) translation proxy for isair/jarvis.

Jarvis talks the Ollama HTTP API. This proxy sits on port 11435, accepts
Ollama-shaped requests, forwards chat/generate calls to Z.ai's
OpenAI-compatible chat completions endpoint, and translates the responses
back into the Ollama shapes Jarvis expects. Embedding calls (and any other
unrecognized Ollama endpoint, e.g. /api/tags) are forwarded unchanged to a
REAL local Ollama instance, which stays responsible for nomic-embed-text.

Endpoints implemented:
  POST /api/chat        -> GLM chat completions (stream=false and stream=true/NDJSON)
  POST /api/generate    -> GLM chat completions (prompt wrapped as one user message)
  POST /api/embeddings  -> transparent passthrough to real Ollama
  anything else         -> transparent passthrough to real Ollama (GET/POST)

Environment variables:
  GLM_API_KEY      (REQUIRED) Z.ai API key. Startup fails loudly if missing.
  GLM_API_BASE     default: https://api.z.ai/api/paas/v4
  GLM_MODEL        default: glm-5.2
  REAL_OLLAMA_URL  default: http://127.0.0.1:11434  (for embeddings passthrough)
  PROXY_HOST       default: 127.0.0.1
  PROXY_PORT       default: 11435
  GLM_TIMEOUT      default: 300 (seconds, per upstream request)

Dependencies: Python 3.9+ standard library only.

Run:  GLM_API_KEY=sk-... python ollama_glm_proxy.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen  # tests patch this module's `urlopen`

DEFAULT_GLM_API_BASE = "https://api.z.ai/api/paas/v4"
DEFAULT_GLM_MODEL = "glm-5.2"
DEFAULT_REAL_OLLAMA_URL = "http://127.0.0.1:11434"


# --------------------------------------------------------------------------
# Config helpers (read env at call time so values can change without restart
# in tests; in normal use they are effectively constants)
# --------------------------------------------------------------------------

def glm_api_base() -> str:
    return os.environ.get("GLM_API_BASE", DEFAULT_GLM_API_BASE).rstrip("/")


def glm_chat_url() -> str:
    return glm_api_base() + "/chat/completions"


def glm_model() -> str:
    return os.environ.get("GLM_MODEL", DEFAULT_GLM_MODEL)


def real_ollama_url() -> str:
    return os.environ.get("REAL_OLLAMA_URL", DEFAULT_REAL_OLLAMA_URL).rstrip("/")


def glm_timeout() -> float:
    return float(os.environ.get("GLM_TIMEOUT", "300"))


def require_api_key() -> str:
    key = os.environ.get("GLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GLM_API_KEY environment variable is not set. "
            "Get a key from https://z.ai and run e.g.:\n"
            "  GLM_API_KEY=your-key python ollama_glm_proxy.py"
        )
    return key


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------
# Request translation: Ollama shape -> OpenAI (GLM) shape
# --------------------------------------------------------------------------

def _tool_calls_to_openai(tool_calls):
    """Ollama assistant-message tool_calls -> OpenAI tool_calls.

    Ollama: {"function": {"name": str, "arguments": dict}}          (no id)
    OpenAI: {"id": str, "type": "function",
             "function": {"name": str, "arguments": json-string}}
    """
    out = []
    for i, tc in enumerate(tool_calls or []):
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments", {})
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        out.append({
            "id": tc.get("id") or "call_%d" % i,
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args},
        })
    return out


def _messages_to_openai(messages):
    out = []
    for m in messages or []:
        msg = {"role": m.get("role", "user"), "content": m.get("content") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = _tool_calls_to_openai(m["tool_calls"])
        if msg["role"] == "tool":
            # OpenAI-style APIs want tool results linked back to a call id.
            # Ollama messages may carry tool_call_id or tool_name; do our best.
            msg["tool_call_id"] = m.get("tool_call_id") or m.get("tool_name") or "call_0"
        # Ollama supports an "images" field on messages; GLM-5.2 text chat
        # does not take it in this shape, so it is dropped deliberately.
        out.append(msg)
    return out


def ollama_chat_to_openai(payload):
    """Translate an Ollama POST /api/chat body into a GLM chat completions body."""
    body = {
        "model": glm_model(),
        "messages": _messages_to_openai(payload.get("messages")),
        # Ollama defaults /api/chat to streaming when unspecified.
        "stream": bool(payload.get("stream", True)),
    }
    opts = payload.get("options") or {}
    if opts.get("temperature") is not None:
        body["temperature"] = opts["temperature"]
    # num_ctx has no OpenAI-side equivalent (context size is fixed by the
    # hosted model); "think" is intentionally not forwarded — GLM controls
    # reasoning server-side and rejects unknown fields on some gateways.
    if payload.get("tools"):
        body["tools"] = payload["tools"]  # already OpenAI JSON-schema format
    return body


def ollama_generate_to_openai(payload):
    """Translate an Ollama POST /api/generate body into a chat completions body."""
    messages = []
    if payload.get("system"):
        messages.append({"role": "system", "content": payload["system"]})
    messages.append({"role": "user", "content": payload.get("prompt", "")})
    body = {"model": glm_model(), "messages": messages, "stream": False}
    opts = payload.get("options") or {}
    if opts.get("temperature") is not None:
        body["temperature"] = opts["temperature"]
    return body


# --------------------------------------------------------------------------
# Response translation: OpenAI (GLM) shape -> Ollama shape
# --------------------------------------------------------------------------

def _tool_calls_to_ollama(tool_calls):
    """OpenAI tool_calls (arguments as JSON string) -> Ollama (arguments as dict)."""
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments", "")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except (json.JSONDecodeError, ValueError):
                args = {"__raw_arguments": args}
        entry = {"function": {"name": fn.get("name", ""), "arguments": args}}
        if tc.get("id"):
            entry["id"] = tc["id"]
        out.append(entry)
    return out


def openai_to_ollama_chat_response(data, model_name):
    """Translate a non-streaming GLM chat completion into Ollama /api/chat shape."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    ollama_msg = {
        "role": msg.get("role") or "assistant",
        "content": msg.get("content") or "",
    }
    if msg.get("tool_calls"):
        ollama_msg["tool_calls"] = _tool_calls_to_ollama(msg["tool_calls"])
    usage = data.get("usage") or {}
    return {
        "model": model_name,
        "created_at": _now_iso(),
        "message": ollama_msg,
        "done": True,
        "done_reason": choice.get("finish_reason") or "stop",
        "prompt_eval_count": usage.get("prompt_tokens", 0),
        "eval_count": usage.get("completion_tokens", 0),
    }


def openai_to_ollama_generate_response(data, model_name):
    """Translate a non-streaming GLM chat completion into Ollama /api/generate shape."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {
        "model": model_name,
        "created_at": _now_iso(),
        "response": msg.get("content") or "",
        "done": True,
        "done_reason": choice.get("finish_reason") or "stop",
    }


# --------------------------------------------------------------------------
# Upstream calls
# --------------------------------------------------------------------------

def call_glm(body):
    """POST the OpenAI-shaped body to GLM. Returns the open HTTP response.

    Caller must close the response. Raises HTTPError/URLError on failure.
    """
    api_key = require_api_key()
    req = Request(
        glm_chat_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    return urlopen(req, timeout=glm_timeout())


def forward_to_real_ollama(method, path, raw_body=None, content_type="application/json"):
    """Transparent passthrough (embeddings, tags, etc.). Returns (status, bytes)."""
    url = real_ollama_url() + path
    headers = {"Content-Type": content_type} if raw_body is not None else {}
    req = Request(url, data=raw_body, headers=headers, method=method)
    try:
        resp = urlopen(req, timeout=glm_timeout())
        try:
            return resp.status, resp.read()
        finally:
            resp.close()
    except HTTPError as e:
        return e.code, e.read()


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "OllamaGLMProxy/1.0"

    # -- helpers ----------------------------------------------------------

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_raw(self, status, payload, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status, message):
        self._send_json(status, {"error": message})

    def log_message(self, fmt, *args):  # quieter, single-line logs to stderr
        sys.stderr.write("[proxy] %s - %s\n" % (self.address_string(), fmt % args))

    # -- routing ----------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        raw = self._read_body()
        try:
            if path == "/api/chat":
                self._handle_chat(json.loads(raw or b"{}"))
            elif path == "/api/generate":
                self._handle_generate(json.loads(raw or b"{}"))
            elif path in ("/api/embeddings", "/api/embed"):
                self._handle_passthrough("POST", path, raw)
            else:
                self._handle_passthrough("POST", path, raw)
        except json.JSONDecodeError:
            self._send_error_json(400, "invalid JSON in request body")
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            self._send_error_json(e.code, "upstream GLM error %s: %s" % (e.code, detail[:2000]))
        except URLError as e:
            self._send_error_json(502, "cannot reach upstream: %s" % e.reason)
        except RuntimeError as e:  # e.g. missing API key
            self._send_error_json(500, str(e))
        except Exception as e:  # last resort: never leave Jarvis hanging
            self._send_error_json(500, "proxy internal error: %r" % e)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            # Real Ollama answers "Ollama is running" here; some clients probe it.
            self._send_raw(200, b"Ollama is running", "text/plain")
            return
        try:
            status, body = forward_to_real_ollama("GET", path)
            if path == "/api/tags" and status == 200:
                body = self._inject_glm_into_tags(body)
            self._send_raw(status, body)
        except URLError:
            if path == "/api/tags":
                # Real Ollama down: still advertise the GLM model so model
                # listings don't come back empty.
                self._send_json(200, {"models": [self._fake_tag_entry()]})
            else:
                self._send_error_json(502, "real Ollama unreachable at %s" % real_ollama_url())

    # -- endpoint handlers -------------------------------------------------

    def _handle_chat(self, payload):
        body = ollama_chat_to_openai(payload)
        model_name = payload.get("model") or glm_model()
        if body["stream"]:
            self._handle_chat_streaming(body, model_name)
        else:
            resp = call_glm(body)
            try:
                data = json.loads(resp.read())
            finally:
                resp.close()
            self._send_json(200, openai_to_ollama_chat_response(data, model_name))

    def _handle_chat_streaming(self, body, model_name):
        resp = call_glm(body)  # SSE stream: lines of "data: {...}", ends "data: [DONE]"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        pending_tool_calls = {}  # index -> {"id":.., "name":.., "arguments": str}
        done_reason = "stop"
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    done_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                # Accumulate incremental tool-call deltas (OpenAI streaming style).
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = pending_tool_calls.setdefault(
                        idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                content = delta.get("content")
                if content:
                    self._write_ndjson({
                        "model": model_name,
                        "created_at": _now_iso(),
                        "message": {"role": "assistant", "content": content},
                        "done": False,
                    })
        finally:
            resp.close()

        if pending_tool_calls:
            openai_style = [
                {"id": slot["id"],
                 "function": {"name": slot["name"], "arguments": slot["arguments"]}}
                for _, slot in sorted(pending_tool_calls.items())
            ]
            self._write_ndjson({
                "model": model_name,
                "created_at": _now_iso(),
                "message": {"role": "assistant", "content": "",
                            "tool_calls": _tool_calls_to_ollama(openai_style)},
                "done": False,
            })

        self._write_ndjson({
            "model": model_name,
            "created_at": _now_iso(),
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": done_reason,
        })

    def _write_ndjson(self, obj):
        self.wfile.write(json.dumps(obj).encode("utf-8") + b"\n")
        self.wfile.flush()

    def _handle_generate(self, payload):
        body = ollama_generate_to_openai(payload)
        model_name = payload.get("model") or glm_model()
        resp = call_glm(body)
        try:
            data = json.loads(resp.read())
        finally:
            resp.close()
        out = openai_to_ollama_generate_response(data, model_name)
        # Jarvis calls /api/generate non-streaming, but Ollama's default for
        # this endpoint is streaming — honor stream=true with a single chunk
        # plus done line so any caller that iterates lines still works.
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            chunk = dict(out)
            chunk["done"] = False
            self._write_ndjson(chunk)
            final = dict(out)
            final["response"] = ""
            self._write_ndjson(final)
        else:
            self._send_json(200, out)

    def _handle_passthrough(self, method, path, raw):
        try:
            status, body = forward_to_real_ollama(method, path, raw)
            self._send_raw(status, body)
        except URLError:
            self._send_error_json(
                502,
                "real Ollama unreachable at %s (needed for %s). "
                "Start Ollama, or set REAL_OLLAMA_URL." % (real_ollama_url(), path),
            )

    def _fake_tag_entry(self):
        return {
            "name": glm_model(),
            "model": glm_model(),
            "modified_at": _now_iso(),
            "size": 0,
            "digest": "remote-glm-proxy",
            "details": {"family": "glm", "parameter_size": "remote", "format": "api"},
        }

    def _inject_glm_into_tags(self, body):
        try:
            tags = json.loads(body)
            models = tags.get("models") or []
            if not any(m.get("name", "").startswith(glm_model()) for m in models):
                models.append(self._fake_tag_entry())
            tags["models"] = models
            return json.dumps(tags).encode("utf-8")
        except Exception:
            return body


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    try:
        require_api_key()  # fail loudly at startup, not on first request
    except RuntimeError as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(1)

    host = os.environ.get("PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("PROXY_PORT", "11435"))
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    sys.stderr.write(
        "Ollama->GLM proxy listening on http://%s:%d\n"
        "  chat/generate  -> %s (model: %s)\n"
        "  embeddings etc -> %s\n" % (host, port, glm_chat_url(), glm_model(), real_ollama_url())
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("shutting down\n")
        server.shutdown()


if __name__ == "__main__":
    main()
