"""'Pon un podcast': newest episodes from the creators you follow that you haven't played yet.

Sources, tried in order per show:
  1. YouTube RSS feed (no key; YouTube has been returning 404s for it since 2026-10-04)
  2. YouTube Data API v3 (free key; `run-pakito.bat youtube` to add one)
  3. Spotify (if connected): the show's latest episodes; opened on YouTube via search, or played on Spotify
"Haven't played" = not opened through Pakito, plus anything you said you already heard. Shorts are skipped.
"""
from __future__ import annotations

import re

import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from difflib import SequenceMatcher

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
FEED = "https://www.youtube.com/feeds/videos.xml?"


def fetch(feed: str) -> str:
    req = urllib.request.Request(FEED + feed, headers={"User-Agent": "Mozilla/5.0 Pakito"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def youtube_api(show: dict, key: str) -> list[dict]:
    """Uploads via the Data API (1 quota unit per call; 10,000 free per day)."""
    feed = show["feed"]
    pid = feed.split("=", 1)[1]
    candidates = [pid] if feed.startswith("playlist_id") else ["UULF" + pid[2:], "UU" + pid[2:]]  # UULF = no Shorts
    last = None
    for playlist in candidates:
        url = ("https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&maxResults=10&"
               + urllib.parse.urlencode({"playlistId": playlist, "key": key}))
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                data = json.loads(r.read())
        except Exception as e:
            last = e
            continue
        out = []
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            vid = (sn.get("resourceId") or {}).get("videoId")
            if vid and "#shorts" not in sn.get("title", "").lower() and sn.get("title") != "Private video":
                out.append({"id": vid, "title": sn["title"], "show": show["name"], "published": sn.get("publishedAt", ""),
                            "url": f"https://www.youtube.com/watch?v={vid}"})
        return out
    raise last or RuntimeError("no playlist")


def spotify_episodes(show: dict, sp) -> list[dict]:
    """The show's latest episodes from Spotify (search by name)."""
    q = show.get("spotify") or show["name"]
    res = sp.search(q=q, limit=1, type="show")
    items = [i for i in (res.get("shows") or {}).get("items", []) if i]
    if not items:
        raise RuntimeError(f"not on Spotify as '{q}'")
    eps = sp.show_episodes(items[0]["id"], limit=10) or {}
    out = []
    for e in eps.get("items", []) or []:
        if not e:
            continue
        out.append({"id": "sp:" + e["id"], "title": e["name"], "show": show["name"],
                    "published": e.get("release_date", "") + ("T12:00:00+00:00" if len(e.get("release_date", "")) == 10 else ""),
                    "url": "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": e["name"]}),
                    "spotify_uri": e.get("uri")})
    return out


def parse(xml: str, show: str) -> list[dict]:
    out = []
    root = ET.fromstring(xml)
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        link = (e.find("a:link", NS).get("href") if e.find("a:link", NS) is not None else "") or ""
        title = e.findtext("a:title", default="", namespaces=NS)
        if not vid or "/shorts/" in link or "#shorts" in title.lower():
            continue
        out.append({"id": vid, "title": title, "show": show,
                    "published": e.findtext("a:published", default="", namespaces=NS),
                    "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def age(published: str, now: float) -> str:
    try:
        t = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return ""
    days = int((now - t) // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


STOP = {"the", "one", "uno", "con", "del", "los", "las", "que", "pon", "ponme", "episodio", "episode", "podcast",
        "with", "por", "para", "una", "ese", "este", "nuevo", "new", "latest", "último"}


class Podcasts:
    def __init__(self, shows: list[dict], heard: set[str], fetcher=fetch, clock=time.time,
                 api_key: str | None = None, spotify=None, api=youtube_api, sp_eps=spotify_episodes):
        self.shows = shows
        self.heard = heard
        self.fetcher = fetcher
        self.api_key, self.spotify = api_key, spotify          # spotify = a spotipy client (or None)
        self.api, self.sp_eps = api, sp_eps
        self.clock = clock
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self.choices: list[dict] = []

    def _episodes(self, show: dict) -> list[dict]:
        hit = self._cache.get(show["feed"])
        if hit and self.clock() - hit[0] < 1800:
            return hit[1]
        errors = []
        eps = None
        try:
            eps = parse(self.fetcher(show["feed"]), show["name"])
        except Exception as e:
            errors.append(f"rss {str(e)[:40]}")
        if eps is None and self.api_key:
            try:
                eps = self.api(show, self.api_key)
            except Exception as e:
                errors.append(f"api {str(e)[:40]}")
        if eps is None and self.spotify is not None:
            try:
                eps = self.sp_eps(show, self.spotify)
            except Exception as e:
                errors.append(f"spotify {str(e)[:40]}")
        if eps is None:
            raise RuntimeError("; ".join(errors) or "no source")
        self._cache[show["feed"]] = (self.clock(), eps)
        return eps

    def options(self, creator: str | None = None, count: int = 3) -> dict:
        shows = self.shows
        if creator:
            c = creator.lower()
            shows = [s for s in self.shows if c in s["name"].lower()
                     or SequenceMatcher(None, c, s["name"].lower()).ratio() > 0.5] or self.shows
        eps, errors = [], []
        for s in shows:
            try:
                eps += [e for e in self._episodes(s) if e["id"] not in self.heard]
            except Exception as ex:
                errors.append(f"{s['name']}: {str(ex)[:60]}")
        eps.sort(key=lambda e: e["published"], reverse=True)
        # mix shows: newest of each first, then the rest
        firsts, seen = [], set()
        for e in eps:
            if e["show"] not in seen:
                firsts.append(e)
                seen.add(e["show"])
        picks = (firsts + [e for e in eps if e not in firsts])[:count]
        self.choices = picks
        out = {"options": [{"n": i + 1, "show": e["show"], "title": e["title"], "when": age(e["published"], self.clock())}
                           for i, e in enumerate(picks)]}
        if errors:
            out["couldnt_check"] = errors
        if not picks:
            out["note"] = "Nothing new that he hasn't played."
        return out

    def find(self, words: str) -> dict | None:
        """An episode he names directly ('el de Dana White en Diary of a CEO'), searched across every show's
        recent episodes, heard or not. Needs most of his meaningful words in the show + title."""
        ws = [w for w in re.findall(r"\w+", words.lower()) if len(w) > 2 and w not in STOP]
        if not ws:
            return None
        best, best_score = None, 0.0
        for s in self.shows:
            try:
                eps = self._episodes(s)
            except Exception:
                continue
            for e in eps:
                hay = f"{e['show']} {e['title']}".lower()
                score = sum(w in hay for w in ws) / len(ws)
                if score > best_score:
                    best, best_score = e, score
        return best if best_score >= 0.6 else None

    def pick(self, choice: str | float) -> dict | None:
        """1-based number, or words from the title/show ('the David Goggins one'). Without offered options (or
        no match among them), searches all shows."""
        if not self.choices:
            return None if isinstance(choice, (int, float)) or str(choice).strip().isdigit() else self.find(str(choice))
        try:
            i = int(float(choice)) - 1
            return self.choices[i] if 0 <= i < len(self.choices) else None
        except (TypeError, ValueError):
            pass
        c = str(choice).lower()
        scored = [(max(SequenceMatcher(None, c, e["title"].lower()).ratio(),
                       0.9 if any(w in e["title"].lower() for w in c.split() if len(w) > 3) else 0), e)
                  for e in self.choices]
        best = max(scored, key=lambda x: x[0])
        return best[1] if best[0] > 0.4 else self.find(str(choice))


def setup() -> None:
    """run-pakito.bat youtube — add a YouTube Data API key."""
    from pathlib import Path
    print("""YouTube Data API key (free, ~3 min):
  1. console.cloud.google.com → pick or create a project
  2. APIs & Services → Library → "YouTube Data API v3" → Enable
  3. APIs & Services → Credentials → Create credentials → API key → copy it
     (optional: Edit key → API restrictions → YouTube Data API v3)
""")
    key = input("API key: ").strip()
    if not key:
        return
    try:
        eps = youtube_api({"name": "Modern Wisdom", "feed": "channel_id=UCIaH-gZIVC432YRjNVvnyCA"}, key)
        print(f"✅ Works. Latest: {eps[0]['title'] if eps else '(none)'}")
    except Exception as e:
        print(f"❌ {e}")
        return
    local = Path(__file__).with_name("config.local.json")
    cfg = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    cfg["youtube_api_key"] = key
    local.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print("Saved. Restart Pakito.")


if __name__ == "__main__":
    setup()
