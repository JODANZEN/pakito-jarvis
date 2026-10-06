# Pakito assistant — v0 (hub + focus trainer + learning)

There's no voice yet. This is the core that the voice layer will plug into. Every action goes through `Hub.call` and gets logged, which is what the learning and the stats read from.

## Run it

```powershell
cd C:\Users\jdsn2\source\repos\pakito-jarvis
pip install winotify matplotlib      # winotify = Windows toasts (optional, it prints otherwise)
python -m assistant
```

**Free hotkey:** Windows Terminal "quake mode" (Win + `) drops a terminal down from the top of the screen. Run `python -m assistant` in it and it's one keypress away. The real hotkey comes with the voice layer.

## Commands

| Type | Does |
|---|---|
| `focus` / `focus cse 355` / `focus 45 phy 302` | Start a block. With no minutes it uses the trainer's level |
| `d` / `d phone` | Log a distraction |
| `stop` | End the block early, or skip the break |
| `status`, `stats`, `stats 30` | |
| `open <url>` · `app <name>` · `music <q>` · `search <q>` | |
| `routines` · `suggest` · `save 1 [name]` · `nope 1` | |
| anything else, e.g. `phy 302 study` | Runs the matching routine. If there isn't one, it watches what you do next and learns it |

## Focus trainer rules (tune them in `config.local.json` → `"focus"`)
- You start at a 20-min level.
- **Finish a block with ≤2 distractions:** level goes up 2 min.
- **Bail before half the block:** level goes down 2 min.
- **Bail after half:** level stays the same.
- Picking your own minutes gets logged, but it doesn't change the level.
- Breaks are 1/5 of the block (minimum 5 min). Every 4th finished block gets a 15-min break.
- `python plot_focus.py` plots planned vs actual minutes, your level over time, and focused minutes per day.

## Learning
1. Type something that isn't a routine (e.g. `ethics post`).
2. Do the steps with commands: `open ...`, `music ...`, `focus ...`.
3. Type `done`, or just make your next request.
4. After the same actions follow 3 times, you get a "Save as routine?" toast. `suggest` and then `save 1` writes it to `Routines.md` in your vault.

## Files (in the vault, under `Assistant/`)
- `Routines.md`: edit in Obsidian. It starts with your Fall 2026 courses.
- `logs/events.jsonl`: every action.
- `logs/focus.jsonl`: every block.
- `logs/episodes.jsonl`: requests plus what followed.
- `state/`: trainer level and dismissed suggestions.

The vault path defaults to `C:\Users\jdsn2\OneDrive\Second Brain`. Override it with env `PAKITO_VAULT` or `assistant/config.local.json`:

```json
{ "vault_path": "D:\\somewhere", "focus": { "start_level_min": 25 } }
```

## Start with Windows
Run `install-startup.bat` once. Pakito then opens minimized every time you log in, so Ctrl+Alt always works. Undo it with `install-startup.bat remove`.

## Debrief
- **"Terminé" / "done for today" / "debrief":** stops any running block, pauses music, reads out your day (blocks, focused minutes, distractions, level change), and saves `Assistant/Days/<date>.md`.
- **"How's my day going?":** the same numbers, but nothing stops.
- Typed: `today` · `debrief`

## Browser
Web links open in **Opera GX** if it's installed (`"browser": "auto"`), so you're already logged in everywhere. Set `"browser": "default"` for the Windows default, or give the full path to any browser's .exe. Pakito's startup line shows which one it found.

## Spotify (real play/pause/skip)
Run `run-pakito.bat spotify` once:
1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → **Create app**.
2. Name it anything. Set Redirect URI to `http://127.0.0.1:8888/callback` (it has to be exactly that; `localhost` isn't allowed). Tick **Web API**, then Save.
3. Open the app's settings, copy the **Client ID**, and paste it when asked. A browser tab opens to log in. After that it's remembered (in `assistant/.spotify_cache`, which isn't synced or committed).

Then you can say:
- "pon mi playlist de gym": your own playlists match first.
- "pon Bad Bunny", "pon un podcast de física".
- "pausa", "siguiente", "súbele", "volume 30", "¿qué canción es esta?"

Spotify's rule since Feb 2026: developer apps only work if **the app owner has Premium**. Without Premium, Pakito still finds the exact playlist or song and opens it in Spotify, but you press play yourself.

## Textbooks
`"books"` in config maps names to PDFs. Say "abre el Taylor" or "open Taylor page 161". Pages are the PDF's page numbers, not the book's printed ones. PDFs open in Opera GX, so jumping to a page works. Routines can use `- book: taylor` or `- book: taylor 161`.

## Lock-in mode
The `lock in` routine, or saying "lock in", starts a focus block where the apps in `"focus" → "block_apps"` (default: `Solitaire.exe`) get closed as soon as they open, until your break. Each attempt:
- counts as a distraction in the trainer;
- shows a toast with how long until the break.

To add more, use process names from Task Manager → Details (e.g. `"steam.exe"`, `"Discord.exe"`).

**Websites too** (YouTube, Instagram, TikTok, X, Reddit, Netflix, Twitch, Facebook by default; edit `"block_sites"`). This needs the small Pakito extension in Opera GX, installed once:
1. Go to `opera://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick the `browser-extension` folder in this repo.

During a lock-in block, those sites show a 🔒 page with how long until your break, and each attempt counts as a distraction. Outside lock-in, or when Pakito isn't running, nothing is blocked. The extension only talks to Pakito on your own PC (`127.0.0.1:8765`).

**Music follows focus:** with Spotify connected, music pauses when a break starts and resumes when the next block starts. It only resumes if Pakito was the one that paused it. Turn this off with `"pause_music_on_break": false`.

## Short-term memory
- Pakito won't reopen the same tab, app, music or search within 30 min. It says it's already open instead. "Open it again" makes Gemini pass `force=true`.
- Each conversation starts with up to 8 lines of what was done in the last 2 h, so it knows what's already happening.
- Typed: `recent`. What you said in each conversation goes to `logs/conversations.jsonl`.
- Tune it under `"memory"` in `config.local.json`.

## Ask Claude (uses your Claude plan via Claude Code, no API key)
1. PowerShell: `irm https://claude.ai/install.ps1 | iex`
2. Open a new terminal, run `claude`, log in in the browser, then `/exit`.
3. `run-pakito.bat check claude` should print "Pakito link works."

Then say "ask Claude …", or Gemini decides to for hard questions. It runs in the background:
- if you're still talking, Gemini says the answer;
- if not, you get a toast and it's mentioned at the start of your next conversation;
- every answer is saved to `Assistant/Claude/<date>.md`.

Typed: `claude <question>`. It counts against your Claude plan's usage, and it isn't available on the free claude.ai plan.

## Tests
`python -m pytest tests`: 57 tests. They use a fake clock and a fake Gemini session, so nothing launches and nothing hits the network.

## Voice (Gemini Live)

```powershell
setx GEMINI_API_KEY "your-key"          # once, then open a NEW terminal
pip install -r assistant\requirements.txt
python -m assistant.voice
```

- **Hold Ctrl+Alt and talk, then release** (push-to-talk). The first hold opens the conversation (rising beep). After that it stays open about 60 s for follow-ups, so the next hold is instant.
- **Hold it while Pakito is talking** to cut it off. **Tap it** to hang up. Saying "that's all" / "gracias" also hangs up.
- The mic only sends while the key is held, so there's no echo problem on laptop speakers.
- The same window still takes typed commands (`focus`, `d`, `stop`, `suggest`...). Run either `assistant.voice` or `assistant`, not both, because the two would keep separate focus timers.
- What you say plus the tools it calls feed the same learner, so voice requests become routine suggestions too.
- Google Search is built in, so weather and questions get answered out loud. `web_search` only opens results on screen.
- **Different key:** set `"hotkey"` in `config.local.json`, e.g. `"right ctrl"`, `"f8"`, `"ctrl+space"`. If you press a third key while holding Ctrl+Alt (Ctrl+Alt+Del, say), it's treated as a shortcut, not talking. Ctrl+Space also triggers VS Code autocomplete, and Alt+Space opens the window menu, which is why neither is the default.
- `"mode": "toggle"` brings back press-once, hands-free listening.

Settings in `assistant/config.local.json`:

```json
{ "voice": { "hotkey": "ctrl+alt", "mode": "ptt", "voice_name": "Puck", "idle_timeout_sec": 60 },
  "browser": "auto" }
```

Free tier: Google may use what you send to improve its products. Keep that in mind once email and WhatsApp tools get added.
