"""Nov 20: 'timed out during opening handshake' killed the turn when one connect attempt hung."""
import asyncio
import contextlib

import pytest

from assistant import voice


class FakeLive:
    def __init__(self, plan):
        self.plan, self.calls = list(plan), 0

    def connect(self, model, config):
        self.calls += 1
        step = self.plan.pop(0)
        outer = self

        class CM:
            async def __aenter__(self):
                if step == "hang":
                    await asyncio.sleep(10)
                if isinstance(step, BaseException):
                    raise step
                return f"session{outer.calls}"

            async def __aexit__(self, *a):
                return False
        return CM()


class FakeClient:
    def __init__(self, plan):
        self.aio = type("A", (), {})()
        self.aio.live = FakeLive(plan)


async def _open(plan, tries=(0.05, 0.05, 0.05)):
    client, said = FakeClient(plan), []
    async with contextlib.AsyncExitStack() as stack:
        s = await voice.open_live(stack, client, "m", {}, tries=tries, say=said.append)
    return s, said, client.aio.live.calls


def test_hung_connect_is_retried():
    s, said, calls = asyncio.run(_open(["hang", TimeoutError("timed out during opening handshake"), "ok"]))
    assert s == "session3" and calls == 3 and len(said) == 2 and "trying again" in said[0]


def test_gives_up_after_the_last_try():
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        asyncio.run(_open(["hang", "hang", "hang"]))


def test_quota_errors_are_not_retried():
    class APIError(Exception):
        pass
    with pytest.raises(APIError):
        asyncio.run(_open([APIError("429 quota exceeded"), "ok"]))


def test_network_errors_are_recognized():
    assert voice.is_network_error(TimeoutError("timed out during opening handshake"))
    assert voice.is_network_error(ConnectionResetError()) and voice.is_network_error(asyncio.TimeoutError())
    assert not voice.is_network_error(ValueError("quota"))


def test_addresses_are_raced_once():
    from google.genai import live as glive
    orig = glive.ws_connect
    try:
        voice._race_addresses()
        voice._race_addresses()
        assert glive.ws_connect.keywords["happy_eyeballs_delay"] == 0.25
        assert glive.ws_connect.func is orig                      # wrapped once, not twice
    finally:
        glive.ws_connect = orig
