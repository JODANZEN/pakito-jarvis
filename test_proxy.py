#!/usr/bin/env python3
"""
Tests for ollama_glm_proxy.py — all network calls to GLM / real Ollama are
mocked (unittest.mock patches the module's `urlopen`); the proxy itself runs
as a real HTTP server on an ephemeral localhost port and is exercised over
real HTTP with http.client.

Run:  python -m unittest test_proxy -v
"""

import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import ollama_glm_proxy as proxy


class FakeUpstreamResponse:
    """Stand-in for the object urllib.request.urlopen returns."""

    def __init__(self, body=b"", lines=None, status=200):
        self._body = body
        self._lines = lines or []
        self.status = status

    def read(self):
        return self._body

    def close(self):
        pass

    def __iter__(self):  # streaming path iterates the response line by line
        return iter(self._lines)


def make_fake_urlopen(response, calls):
    """Returns a fake urlopen that records each Request into `calls`."""

    def fake_urlopen(req, **kwargs):
        calls.append(req)
        return response

    return fake_urlopen


class ProxyTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["GLM_API_KEY"] = "test-key-123"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def http_post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body


class TestChatNonStreaming(ProxyTestBase):
    def test_request_and_response_translation(self):
        glm_response = {
            "id": "cc-1",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello from GLM!"},
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }
        calls = []
        fake = make_fake_urlopen(
            FakeUpstreamResponse(body=json.dumps(glm_response).encode()), calls)

        ollama_request = {
            "model": "glm-5.2",
            "messages": [
                {"role": "system", "content": "You are Jarvis."},
                {"role": "user", "content": "Say hello."},
            ],
            "stream": False,
            "options": {"num_ctx": 8192, "temperature": 0.4},
            "think": False,
        }
        with mock.patch.object(proxy, "urlopen", fake):
            status, body = self.http_post("/api/chat", ollama_request)

        self.assertEqual(status, 200)

        # --- outbound request was translated to OpenAI/GLM shape ---
        self.assertEqual(len(calls), 1)
        req = calls[0]
        self.assertEqual(req.full_url, "https://api.z.ai/api/paas/v4/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key-123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        sent = json.loads(req.data)
        self.assertEqual(sent["model"], "glm-5.2")
        self.assertEqual(sent["messages"], ollama_request["messages"])
        self.assertIs(sent["stream"], False)
        self.assertEqual(sent["temperature"], 0.4)
        # Ollama-only fields must NOT leak through to GLM
        for forbidden in ("options", "num_ctx", "think"):
            self.assertNotIn(forbidden, sent)

        # --- inbound response was translated back to Ollama shape ---
        out = json.loads(body)
        self.assertEqual(out["message"]["role"], "assistant")
        self.assertEqual(out["message"]["content"], "Hello from GLM!")
        self.assertIs(out["done"], True)
        self.assertEqual(out["done_reason"], "stop")
        self.assertEqual(out["model"], "glm-5.2")

    def test_tools_passthrough_and_tool_calls_translation(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }]
        glm_response = {
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": "{\"city\": \"Tempe\"}",  # JSON STRING (OpenAI style)
                        },
                    }],
                },
            }],
        }
        calls = []
        fake = make_fake_urlopen(
            FakeUpstreamResponse(body=json.dumps(glm_response).encode()), calls)

        with mock.patch.object(proxy, "urlopen", fake):
            status, body = self.http_post("/api/chat", {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "Weather in Tempe?"}],
                "stream": False,
                "tools": tools,
            })

        self.assertEqual(status, 200)
        sent = json.loads(calls[0].data)
        self.assertEqual(sent["tools"], tools)  # untouched passthrough

        out = json.loads(body)
        tcs = out["message"]["tool_calls"]
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "get_weather")
        # Ollama shape: arguments is a DICT, not a JSON string
        self.assertEqual(tcs[0]["function"]["arguments"], {"city": "Tempe"})

    def test_assistant_tool_calls_in_history_converted_to_openai(self):
        """Round-trip: Ollama history with dict arguments -> OpenAI string args."""
        glm_response = {"choices": [{"message": {"role": "assistant",
                                                 "content": "72F and sunny."},
                                     "finish_reason": "stop"}]}
        calls = []
        fake = make_fake_urlopen(
            FakeUpstreamResponse(body=json.dumps(glm_response).encode()), calls)

        with mock.patch.object(proxy, "urlopen", fake):
            status, _ = self.http_post("/api/chat", {
                "model": "glm-5.2",
                "stream": False,
                "messages": [
                    {"role": "user", "content": "Weather in Tempe?"},
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "get_weather",
                                                  "arguments": {"city": "Tempe"}}}]},
                    {"role": "tool", "content": "{\"temp_f\": 72}",
                     "tool_name": "get_weather"},
                ],
            })

        self.assertEqual(status, 200)
        sent = json.loads(calls[0].data)
        assistant_msg = sent["messages"][1]
        tc = assistant_msg["tool_calls"][0]
        self.assertEqual(tc["type"], "function")
        self.assertTrue(tc["id"])  # id synthesized since Ollama shape has none
        self.assertIsInstance(tc["function"]["arguments"], str)
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"city": "Tempe"})
        tool_msg = sent["messages"][2]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("tool_call_id", tool_msg)

    def test_upstream_http_error_returns_json_error(self):
        from urllib.error import HTTPError
        import io

        def raising_urlopen(req, **kw):
            raise HTTPError(req.full_url, 401, "Unauthorized", {},
                            io.BytesIO(b'{"error": "bad key"}'))

        with mock.patch.object(proxy, "urlopen", raising_urlopen):
            status, body = self.http_post("/api/chat", {
                "model": "glm-5.2", "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(status, 401)
        self.assertIn("error", json.loads(body))


class TestChatStreaming(ProxyTestBase):
    def test_sse_to_ndjson_translation(self):
        sse_lines = [
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n',
            b'\n',
            b'data: {"choices":[{"index":0,"delta":{"content":"lo "}}]}\n',
            b'data: {"choices":[{"index":0,"delta":{"content":"world"},"finish_reason":"stop"}]}\n',
            b'data: [DONE]\n',
        ]
        calls = []
        fake = make_fake_urlopen(FakeUpstreamResponse(lines=sse_lines), calls)

        with mock.patch.object(proxy, "urlopen", fake):
            status, body = self.http_post("/api/chat", {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "Say hello world."}],
                "stream": True,
            })

        self.assertEqual(status, 200)
        sent = json.loads(calls[0].data)
        self.assertIs(sent["stream"], True)

        lines = [json.loads(l) for l in body.decode().strip().split("\n")]
        self.assertGreaterEqual(len(lines), 2)
        content = "".join(l["message"]["content"] for l in lines if not l["done"])
        self.assertEqual(content, "Hello world")
        for l in lines[:-1]:
            self.assertIs(l["done"], False)
        final = lines[-1]
        self.assertIs(final["done"], True)
        self.assertEqual(final["done_reason"], "stop")
        self.assertEqual(final["message"]["content"], "")

    def test_streaming_tool_call_deltas_are_accumulated(self):
        sse_lines = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"get_weather","arguments":""}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\""}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":": \\"Tempe\\"}"}}]},"finish_reason":"tool_calls"}]}\n',
            b'data: [DONE]\n',
        ]
        fake = make_fake_urlopen(FakeUpstreamResponse(lines=sse_lines), [])

        with mock.patch.object(proxy, "urlopen", fake):
            status, body = self.http_post("/api/chat", {
                "model": "glm-5.2", "stream": True,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [{"type": "function",
                           "function": {"name": "get_weather", "parameters": {}}}],
            })

        self.assertEqual(status, 200)
        lines = [json.loads(l) for l in body.decode().strip().split("\n")]
        tool_lines = [l for l in lines if l["message"].get("tool_calls")]
        self.assertEqual(len(tool_lines), 1)
        tc = tool_lines[0]["message"]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "get_weather")
        self.assertEqual(tc["function"]["arguments"], {"city": "Tempe"})
        self.assertIs(lines[-1]["done"], True)


class TestGenerate(ProxyTestBase):
    def test_prompt_wrapped_and_response_translated(self):
        glm_response = {"choices": [{"message": {"role": "assistant",
                                                 "content": "cleaned text"},
                                     "finish_reason": "stop"}]}
        calls = []
        fake = make_fake_urlopen(
            FakeUpstreamResponse(body=json.dumps(glm_response).encode()), calls)

        with mock.patch.object(proxy, "urlopen", fake):
            status, body = self.http_post("/api/generate", {
                "model": "glm-5.2",
                "prompt": "Clean up this dictation: helo wrld",
                "stream": False,
            })

        self.assertEqual(status, 200)
        sent = json.loads(calls[0].data)
        self.assertEqual(sent["messages"],
                         [{"role": "user",
                           "content": "Clean up this dictation: helo wrld"}])
        self.assertIs(sent["stream"], False)

        out = json.loads(body)
        self.assertEqual(out["response"], "cleaned text")
        self.assertIs(out["done"], True)

    def test_system_field_becomes_system_message(self):
        glm_response = {"choices": [{"message": {"content": "ok"},
                                     "finish_reason": "stop"}]}
        calls = []
        fake = make_fake_urlopen(
            FakeUpstreamResponse(body=json.dumps(glm_response).encode()), calls)

        with mock.patch.object(proxy, "urlopen", fake):
            status, _ = self.http_post("/api/generate", {
                "model": "glm-5.2", "prompt": "hi",
                "system": "Be terse.", "stream": False,
            })

        self.assertEqual(status, 200)
        sent = json.loads(calls[0].data)
        self.assertEqual(sent["messages"][0],
                         {"role": "system", "content": "Be terse."})
        self.assertEqual(sent["messages"][1], {"role": "user", "content": "hi"})


class TestEmbeddingsPassthrough(ProxyTestBase):
    def test_forwarded_unchanged_to_real_ollama(self):
        ollama_reply = {"embedding": [0.1, -0.2, 0.3]}
        calls = []
        fake = make_fake_urlopen(
            FakeUpstreamResponse(body=json.dumps(ollama_reply).encode()), calls)

        request_payload = {"model": "nomic-embed-text", "prompt": "hello world"}
        with mock.patch.object(proxy, "urlopen", fake):
            status, body = self.http_post("/api/embeddings", request_payload)

        self.assertEqual(status, 200)
        req = calls[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/embeddings")
        self.assertEqual(json.loads(req.data), request_payload)  # byte-identical forward
        self.assertIsNone(req.get_header("Authorization"))  # no GLM key leaks to Ollama
        self.assertEqual(json.loads(body), ollama_reply)


class TestTranslationUnits(unittest.TestCase):
    """Pure-function checks that don't need the HTTP server."""

    def test_missing_api_key_fails_loudly(self):
        with mock.patch.dict(os.environ, {"GLM_API_KEY": ""}):
            with self.assertRaises(RuntimeError) as ctx:
                proxy.require_api_key()
            self.assertIn("GLM_API_KEY", str(ctx.exception))

    def test_malformed_tool_arguments_do_not_crash(self):
        out = proxy._tool_calls_to_ollama([
            {"id": "x", "function": {"name": "f", "arguments": "{not json"}}])
        self.assertEqual(out[0]["function"]["arguments"],
                         {"__raw_arguments": "{not json"})

    def test_empty_glm_choices_yield_empty_message(self):
        out = proxy.openai_to_ollama_chat_response({"choices": []}, "glm-5.2")
        self.assertEqual(out["message"]["content"], "")
        self.assertIs(out["done"], True)

    def test_chat_defaults_to_streaming_like_ollama(self):
        body = proxy.ollama_chat_to_openai(
            {"messages": [{"role": "user", "content": "hi"}]})
        self.assertIs(body["stream"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
