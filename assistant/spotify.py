"""Real Spotify control through the Web API (spotipy, PKCE login — no client secret).

Setup once:  run-pakito.bat spotify
  1. developer.spotify.com/dashboard → Create app → Redirect URI: http://127.0.0.1:8888/callback
     → tick "Web API" → Save → copy the Client ID.
  2. Paste it when asked. A browser tab opens to log in; after that it's remembered.

Spotify rule (Feb 2026): developer-mode apps only work if the app owner has Premium.
Without Premium, search still works (Pakito opens the exact playlist/song) but it can't press play.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing playlist-read-private"
REDIRECT = "http://127.0.0.1:8888/callback"
CACHE = Path(__file__).with_name(".spotify_cache")          # gitignored; stays out of the synced vault
KINDS = {"playlist": "playlist", "track": "track", "song": "track", "artist": "artist", "album": "album",
         "podcast": "show", "show": "show", "episode": "episode"}


def _session():
    """requests session that survives Spotify dropping idle connections.

    spotipy's own session never retries a dropped connection (read=False), so the first request after
    a quiet stretch died with 'Connection aborted / RemoteDisconnected'. This one reconnects once,
    and never sleeps through a long Retry-After (that froze Pakito for ~30 s on a 429)."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry = Retry(total=2, connect=2, read=1, status=1, backoff_factor=0.2,
                  status_forcelist=(500, 502, 503, 504), respect_retry_after_header=False,
                  allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]), raise_on_status=False)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def make_client(client_id: str, open_browser: bool = True):
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE

    session = _session()
    auth = SpotifyPKCE(client_id=client_id, redirect_uri=REDIRECT, scope=SCOPES, cache_path=str(CACHE),
                       open_browser=open_browser, requests_session=session, requests_timeout=5)
    # fail fast: when Spotify's servers hiccup we fall back instead of retrying for seconds
    return spotipy.Spotify(auth_manager=auth, requests_session=session, requests_timeout=5)


HITS = Path(__file__).with_name(".spotify_hits.json")     # remembered search results (gitignored with the cache)


class SpotifyControl:
    def __init__(self, sp, launch: Callable[[str], None], sleep=time.sleep, hits_path: Path | None = HITS,
                 clock=time.time):
        self.sp = sp
        self.launch = launch
        self.sleep = sleep
        self.clock = clock
        self.hits_path = hits_path
        self._mine: tuple[float, list] | None = None
        self.hits: dict = {}
        if hits_path and Path(hits_path).exists():
            try:
                self.hits = json.loads(Path(hits_path).read_text(encoding="utf-8"))
            except Exception:
                self.hits = {}

    def _remember(self, key: str, hit: dict) -> None:
        self.hits[key] = hit
        if self.hits_path:
            try:
                Path(self.hits_path).write_text(json.dumps(self.hits, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    # ---- finding things ----
    def _own_playlist(self, query: str):
        q = query.lower()
        best, score = None, 0.0
        if not self._mine or self.clock() - self._mine[0] > 600:      # your playlists: refresh every 10 min
            self._mine = (self.clock(), (self.sp.current_user_playlists(limit=50) or {}).get("items", []) or [])
        for p in self._mine[1]:
            if not p:
                continue
            name = (p.get("name") or "").lower()
            s = SequenceMatcher(None, q, name).ratio()
            if q in name or name in q:
                s = max(s, 0.9)
            if s > score:
                best, score = p, s
        return best if score >= 0.75 else None

    def find(self, query: str, kind: str = "playlist") -> dict | None:
        """-> {'uri', 'name', 'type'} for the best match. Your own playlists win for playlist searches.
        Results are remembered, so "pon música" doesn't depend on Spotify's search being up."""
        kind = KINDS.get((kind or "playlist").lower(), "playlist")
        key = f"{kind}:{query.lower().strip()}"
        try:
            hit = self._find_live(query, kind)
        except Exception:
            if key in self.hits:
                return {**self.hits[key], "remembered": True}
            raise
        if hit:
            self._remember(key, hit)
        return hit

    def _find_live(self, query: str, kind: str) -> dict | None:
        if kind == "playlist":
            mine = self._own_playlist(query)
            if mine:
                return {"uri": mine["uri"], "name": mine["name"], "type": "playlist", "yours": True}
        title, artist = split_by(query) if kind in ("track", "album") else (query, None)
        q = f'{kind if kind == "album" else "track"}:{title} artist:{artist}' if artist else query
        res = self.sp.search(q=q, limit=10, type=kind)
        items = [i for i in (res.get(f"{kind}s") or {}).get("items", []) if i]
        if not items and artist:                       # field search too strict: plain search
            res = self.sp.search(q=query, limit=10, type=kind)
            items = [i for i in (res.get(f"{kind}s") or {}).get("items", []) if i]
        if not items:
            return None
        scored = sorted(((match_score(i, title, artist), n, i) for n, i in enumerate(items)),
                        key=lambda x: (-x[0], x[1]))
        score, _, top = scored[0]
        out = {"uri": top["uri"], "name": top.get("name"), "type": kind}
        if kind in ("track", "album"):
            out["by"] = ", ".join(a["name"] for a in top.get("artists", []))
        if kind in ("track", "album", "artist") and score < 0.5:
            out["unsure"] = True                       # Gemini should say what it found and ask
        return out

    # ---- devices ----
    def _device(self) -> str | None:
        devs = [d for d in (self.sp.devices() or {}).get("devices", []) if not d.get("is_restricted")]
        computers = [d for d in devs if d.get("type") == "Computer"]
        active = [d for d in devs if d.get("is_active")]
        pick = [d for d in active if d.get("type") == "Computer"] or active or computers or devs
        return pick[0]["id"] if pick else None

    def _is_playing(self, uri: str | None = None) -> bool:
        cur = self.sp.current_playback() or self.sp.currently_playing() or {}
        if not cur.get("is_playing"):
            return False
        if not uri:
            return True
        item = cur.get("item") or {}
        ctx = (cur.get("context") or {}).get("uri")
        return uri in (item.get("uri"), ctx)

    def _start(self, dev: str, hit: dict) -> None:
        if hit["type"] in ("track", "episode"):
            self.sp.start_playback(device_id=dev, uris=[hit["uri"]])
        else:
            self.sp.start_playback(device_id=dev, context_uri=hit["uri"])

    def _ensure_device(self) -> str | None:
        dev = self._device()
        if dev:
            return dev
        self.launch("spotify:")                    # open the desktop app, then wait for it to show up
        for _ in range(10):
            self.sleep(1)
            dev = self._device()
            if dev:
                return dev
        return None

    # ---- actions ----
    def play(self, query: str, kind: str = "playlist") -> dict:
        try:
            hit = self.find(query, kind)
        except Exception as e:                     # Spotify search down (502/429) and nothing remembered
            self.launch("spotify:search:" + urllib.parse.quote(query))
            return {"opened_search": query, "note": f"Spotify's servers are acting up ({str(e)[:60]}); "
                                                     "opened the search in the app instead. Say so briefly."}
        if not hit:
            return {"error": f"Nothing on Spotify for '{query}'."}
        try:
            dev = self._ensure_device()
            if not dev:
                raise RuntimeError("no device")
            self._start(dev, hit)
            for attempt in range(3):                       # Spotify can say "ok" and play nothing: check
                self.sleep(0.8)
                if self._is_playing(hit["uri"]):
                    return {"playing": hit}
                if attempt == 0:                           # wake the PC app and hand it the playback
                    self.sp.transfer_playback(device_id=dev, force_play=True)
                    self.sleep(0.6)
                    self._start(dev, hit)
                elif attempt == 1:
                    self.launch("spotify:")
                    self.sleep(1.5)
                    dev = self._device() or dev
                    self._start(dev, hit)
            self.launch(hit["uri"])
            return {"opened": hit, "note": "Spotify accepted the command but didn't start playing. Opened it in the "
                                           "Spotify app instead: he may need to press play once to wake it up."}
        except Exception as e:
            # no Premium / no device: at least open the exact thing in the app
            self.launch(hit["uri"])
            return {"opened": hit, "note": f"Opened in Spotify but couldn't start playback ({str(e)[:80]}). "
                                           "Starting playback needs Premium."}

    def control(self, action: str, volume: float | None = None) -> dict:
        sp = self.sp
        action = action.lower()
        if action in ("now_playing", "what", "status"):
            cur = sp.current_playback() or sp.currently_playing() or {}
            item = cur.get("item") or {}
            if not item:
                return {"playing": None}
            by = ", ".join(a["name"] for a in item.get("artists", [])) or (item.get("show") or {}).get("name")
            return {"playing": item.get("name"), "by": by, "is_playing": cur.get("is_playing"),
                    "volume": (cur.get("device") or {}).get("volume_percent")}
        dev = self._device()
        if action in ("pause", "resume", "play"):
            want_playing = action != "pause"
            try:
                (sp.start_playback if want_playing else sp.pause_playback)(device_id=dev)
            except Exception as e:
                if "403" in str(e) or "Restriction" in str(e):   # nothing to pause / already playing
                    playing = self._is_playing()
                    if playing == want_playing:
                        return {"ok": True, "action": action, "note": "it already was"}
                    if not playing and want_playing:
                        return {"error": "Nothing is loaded in Spotify to resume. Ask what to play."}
                raise
            return {"ok": True, "action": action}
        if action in ("next", "skip"):
            sp.next_track(device_id=dev)
        elif action in ("previous", "back"):
            sp.previous_track(device_id=dev)
        elif action in ("volume", "volume_up", "volume_down"):
            cur = ((sp.current_playback() or {}).get("device") or {}).get("volume_percent", 50)
            if volume is None:
                volume = cur + (15 if action == "volume_up" else -15)
            volume = int(max(0, min(100, volume)))
            sp.volume(volume, device_id=dev)
            return {"ok": True, "volume": volume}
        else:
            return {"error": f"unknown action {action}"}
        return {"ok": True, "action": action}


def split_by(query: str) -> tuple[str, str | None]:
    """'Armageddon by Tut Tut Child' / 'Armageddon de Karol G' -> ('Armageddon', 'Tut Tut Child')."""
    import re
    m = re.match(r"^(.*\S)\s+(?:by|de|of)\s+(\S.*)$", query.strip(), re.I)
    return (m.group(1), m.group(2)) if m else (query.strip(), None)


def match_score(item: dict, title: str, artist: str | None) -> float:
    def sim(a: str, b: str) -> float:
        a, b = a.lower(), b.lower()
        if not a or not b:
            return 0.0
        if a in b or b in a:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()
    name = sim(title, item.get("name", ""))
    if not artist:
        return name
    artists = max((sim(artist, a.get("name", "")) for a in item.get("artists", [])), default=0.0)
    return 0.6 * name + 0.4 * artists


def search_uri(query: str) -> str:
    return "spotify:search:" + urllib.parse.quote(query)


def setup() -> None:
    """run-pakito.bat spotify"""
    import json

    local = Path(__file__).with_name("config.local.json")
    cfg = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    print(__doc__)
    cid = input("Client ID: ").strip()
    if not cid:
        return
    cfg.setdefault("spotify", {})["client_id"] = cid
    local.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    sp = make_client(cid)
    me = sp.current_user()
    print(f"\n✅ Logged in as {me.get('display_name')} ({me.get('product', '?')} account)")
    if me.get("product") and me.get("product") != "premium":
        print("   Not Premium: Pakito can find and open exact playlists/songs, but can't press play/pause.")
    devs = sp.devices().get("devices", [])
    print(f"   Devices Spotify sees right now: {[d['name'] for d in devs] or 'none (open the Spotify app)'}")


if __name__ == "__main__":
    setup()
