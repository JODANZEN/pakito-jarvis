"""Pause whatever is playing (a YouTube video, Spotify, a podcast…) while Pakito listens hands-free, and resume it
afterwards. Nov 24: with a video playing, Gemini heard the video as if he were talking and didn't hear him.

Windows only, through the same media controls as the volume flyout (pip: winrt-Windows.Media.Control). Anywhere else,
or without that package, it quietly does nothing.
"""
from __future__ import annotations

import asyncio
import sys

PLAYING, PAUSED = 4, 5


async def _sessions():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    mgr = await Manager.request_async()
    return list(mgr.get_sessions())


def _status(s) -> int:
    try:
        return int(s.get_playback_info().playback_status)
    except Exception:
        return -1


class MediaPauser:
    def __init__(self, sessions=None, enabled: bool = True):
        self._sessions = sessions or _sessions        # tests pass fake sessions
        self.enabled = enabled and (sessions is not None or sys.platform == "win32")

    async def _run(self, coro_fn):
        """WinRT calls get their own event loop in a worker thread: never stalls Pakito's audio loop."""
        return await asyncio.to_thread(lambda: asyncio.run(coro_fn()))

    async def pause_playing(self) -> list[str]:
        """Pause everything that's playing; -> the apps it paused (to resume exactly those)."""
        if not self.enabled:
            return []

        async def go():
            paused = []
            for s in await self._sessions():
                if _status(s) == PLAYING and await s.try_pause_async():
                    paused.append(s.source_app_user_model_id)
            return paused
        try:
            return await asyncio.wait_for(self._run(go), 3)
        except Exception:
            return []                                  # no winrt package / no media: nothing to do

    async def resume(self, apps: list[str]) -> list[str]:
        """Play again what we paused, unless he started something else meanwhile ("pon música")."""
        if not self.enabled or not apps:
            return []

        async def go():
            sessions = await self._sessions()
            if any(_status(s) == PLAYING for s in sessions):
                return []
            resumed = []
            for s in sessions:
                if s.source_app_user_model_id in apps and _status(s) == PAUSED and await s.try_play_async():
                    resumed.append(s.source_app_user_model_id)
            return resumed
        try:
            return await asyncio.wait_for(self._run(go), 3)
        except Exception:
            return []
