# Pakito assistant

A free, fast voice assistant for Windows: Gemini Live on a hotkey, plus a hub of tools (apps, music, focus
trainer with lock-in, routines, notes, reminders, dictation, screen questions, Claude for hard ones).
Every action goes through `Hub.call` and gets logged, which is what the learning and the stats read from.

## Start here (new PC)

1. Download the repo, double-click **`setup-pakito.bat`**. It installs the Python packages (it uses Miniconda if you
   have it, else makes a private `.venv` with Python 3.10–3.13: 3.13 recommended, newer ones lack the mic library), then asks a few questions:
   your name and city, a **free Gemini key** (it opens the page and checks the key), where to keep notes,
   your **browser**, your **mic** (with a level test), **Spotify** (Premium or not), and whether Claude Code is there.
2. Double-click **`run-pakito.bat`**. Hold **Ctrl+Alt**, talk, let go.

Change any answer later with `run-pakito.bat setup` (Enter keeps what you have).

### On a Mac
1. Download the repo (Code → Download ZIP, unzip). In Terminal, in that folder: **`bash setup-pakito.command`**
   (after the first time you can double-click it). It needs Python 3.10–3.13 (`brew install python@3.13`) and
   Homebrew for the mic library (it installs PortAudio for you).
2. **`./run-pakito.command`**. Hold **Ctrl+Option** to talk, **Ctrl+Cmd** to dictate.
3. The first time, macOS asks for **Microphone**, **Accessibility** and **Input Monitoring** for Terminal (System
   Settings → Privacy & Security). Allow them and start it again; if a hotkey does nothing, that's the permission.

Works on Mac: voice, music, focus + lock-in (the browser extension, plus closing blocked tabs in front), routines,
opening apps, notes, reminders, tasks, dictation, "mira esto" (needs Screen Recording), Claude, calendar.
Windows-only for now: Pakibot, pausing during calls, closing blocked apps.

### Two computers, one memory (PC + Mac)
Point both at the same synced folder (your Second Brain on OneDrive). On the second computer, setup finds it
("Pakito's memory from my other computer") and joins it.
- **Shared:** routines, tasks, reminders, notes/inbox, the focus level, dream questions, and the history of what
  you did. "¿Qué hice hoy?" and the dream see both computers.
- **Each computer writes its own logs** (`logs/events.<name>.jsonl`...) so OneDrive never has to merge one file;
  Pakito reads them all. Pet position, Claude jobs and the running focus block stay per computer.
- **Both can stay on.** The one you used last is in charge of reminders. A computer that sleeps stops its
  heartbeat (`state/devices/`), and within ~2 minutes the other takes over. Talk to one and it takes over right away.

### Without a Second Brain
Nothing here needs Obsidian. By default Pakito keeps its files in `Documents\Pakito` as plain Markdown:
`Assistant/Routines.md`, `Tasks.md`, `Reminders.md`, day notes, logs, and `inbox/` for "toma nota". If you do use
Obsidian, point setup at your vault and the same files land inside it. The daily "dream" that files notes into a
Second Brain is a separate scheduled Claude task, not part of Pakito.

| Needs only the Gemini key | Needs an extra |
|---|---|
| talking, Google Search answers, opening apps/sites, routines, focus trainer + lock-in, notes, reminders, tasks, dictation, "mira esto", Pakibot, debrief | Spotify play/pause (Premium + `run-pakito.bat spotify`), lock-in site blocking (the browser extension), "pregúntale a Claude" and builds (Claude Code), calendar (`run-pakito.bat calendar`) |

**Text-only mode** (no voice, typed commands): `run-pakito.bat text`. **Free hotkey for it:** Windows Terminal "quake
mode" (Win + `).

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

## Tasks
`Assistant/Tasks.md` in your vault is an Obsidian checklist that Pakito keeps:
- "Anota que tengo que…" adds a task.
- "Ya terminé…" ticks one off.
- "¿Qué tenemos para hoy?" reads the list.

Ticking a box in Obsidian works too. This is temporary: it moves to Google Tasks so your phone has it (see ROADMAP).

## Spoken debrief + Examen
Say **"terminé"** and Pakito:
1. Reads your focus stats.
2. Asks what you got done and what's still open, then updates the task list. Open items carry over to tomorrow.
3. Offers the **Examen**: the five Ignatian questions, one at a time. It just listens and saves your answers.

Everything lands in `Assistant/Days/<date>.md`: stats, done, still open, notes, Examen. Change the Examen wording with `"examen": [...]` in `config.local.json`.

Privacy: whatever you say goes through Gemini's free tier, and Google may use free-tier content to improve its products. Keep that in mind for the Examen. ROADMAP has the local-only option.

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

## Podcasts ("pon un podcast")
Pakito finds the newest episodes you haven't played from Modern Wisdom (Chris Williamson) and ZZEN Talks and reads you 2–3 titles. You pick: "el segundo", or "the sleep one". It opens on YouTube, or on Spotify with `"podcast_player": "spotify"`.
- "Ya lo escuché" marks an episode as heard, so it isn't offered again.
- "Pon un podcast de ZZEN" offers only that show.
- To add a creator, add an entry to `"podcasts"` in config: their YouTube `channel_id=UC...` or `playlist_id=PL...`, plus their name on Spotify.

Where the episode list comes from, tried in order:
1. YouTube's RSS feed. Free, but it has been down since 2026-10-04.
2. The YouTube Data API. Also free: run `run-pakito.bat youtube` once to add a key.
3. Spotify, which is already connected. It lists the shows' latest episodes and opens your pick as a YouTube search.

"Haven't played" means you haven't opened it through Pakito. Pakito can't see your YouTube watch history.

## Textbooks
`"books"` in config maps names to PDFs. Say "abre el Taylor" or "open Taylor page 161". Pages are the PDF's page numbers, not the book's printed ones. PDFs open in Opera GX, so jumping to a page works. Routines can use `- book: taylor` or `- book: taylor 161`.

## Lock-in mode
The `lock in` routine, or saying "lock in", starts a focus block where the apps in `"focus" → "block_apps"` (default: `Solitaire.exe`) get closed as soon as they open, until your break. Each attempt:
- counts as a distraction in the trainer;
- shows a toast with how long until the break.

To add more, use process names from Task Manager → Details (e.g. `"steam.exe"`, `"Discord.exe"`).

**Websites too** (Instagram, TikTok, X, Reddit, Netflix, Twitch and Facebook by default; edit `"block_sites"`). YouTube stays open because you use it for podcasts and learning. This needs the small Pakito extension in Opera GX, installed once:
1. Go to `opera://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick the `browser-extension` folder in this repo.

During a lock-in block, those sites show a 🔒 page with how long until your break, and each attempt counts as a distraction. Outside lock-in, or when Pakito isn't running, nothing is blocked. The extension only talks to Pakito on your own PC (`127.0.0.1:8765`).

**Phone notifications go quiet:** during lock-in, Phone Link gets closed silently, so your S25's notifications stop popping up on the PC. This doesn't count as a distraction. Phone Link reopens when the break starts. To mute other apps too, add them to `"mute_apps"`, e.g. `"WhatsApp.exe"` or `"Discord.exe"`.

**Music follows focus:** with Spotify connected, music pauses when a break starts and resumes when the next block starts. It only resumes if Pakito was the one that paused it. Turn this off with `"pause_music_on_break": false`.

## Short-term memory
- Pakito won't reopen the same tab, app, music or search within 30 min. It says it's already open instead. "Open it again" makes Gemini pass `force=true`.
- Each conversation starts with up to 8 lines of what was done in the last 2 h, so it knows what's already happening.
- Typed: `recent`. What you said in each conversation goes to `logs/conversations.jsonl`.
- Tune it under `"memory"` in `config.local.json`.

## Opening apps + Claude Code
- **"Abre Claude"** opens the Claude desktop app. If the app isn't installed, it opens claude.ai instead.
- **"Abre <any app>"** works for anything in your Start menu: Discord, Teams, Blender… Pakito asks Windows for its installed-app list once at startup.
- **"Abre Claude Code"** / **"Claude en la terminal"** opens a new terminal window running `claude` in `source\repos`.
  - **"…en pakito"** / **"…en rabbit hole"** opens it inside that project folder (fuzzy match).
  - **"…donde lo dejé"** continues your last session there (`claude --continue`).
  - Change the folder with `"claude": {"projects_dir": "..."}`.
- Typed: `cc` or `cc pakito`.

## Pakito as an MCP server (other brains can use Pakito)
Claude Code, the Claude desktop app and later Hermes can use Pakito's tools: add a task, start a focus block, check your stats, open things on your PC.
- **Setup:** run `run-pakito.bat mcp` once. In Claude Code, Pakito's tools show up as `mcp__pakito__*`. For the desktop app, the command prints a snippet to paste into its config.
- **One Pakito, not two.** The MCP server is a thin relay to the Pakito that's already running, so whoever asks, there's one focus timer, one task list and one log. Every action shows up in `events.jsonl` with `"source": "mcp"`. If Pakito isn't running, the tools say so instead of doing anything.
- **Security:**
  - The relay talks to Pakito on `127.0.0.1:8765` with a secret token from `assistant/.pakito_token`. It's created on first start and is gitignored.
  - Browser requests are refused, so a website can't make Pakito do things.
- **Voice-only tools:** `ask_claude` and `open_claude_code` can't be used through MCP, so an agent can't start more agents.

## Notes and reminders
- **"Toma nota: …" / "anota esta idea"** saves your words as a raw capture in your vault's **inbox**, as `inbox/YYYY-MM-DD <topic>.md`. It follows the vault rules: your phrasing is kept, the note links up to Home, and the weekly sweep files it. More notes on the same topic that day go into the same file.
  - **Long notes are safe.** Pakito keeps its own recording of what you say. When it saves a note it transcribes *that recording* word for word, in 2-minute pieces, in the background. Gemini no longer retypes (or summarizes) it. Talk for as long as you want; a toast says when it's in the inbox.
  - You can say "toma nota" first and the note in your next turn. If you only announce it, Pakito says it's listening and waits.
  - The audio is written to `Assistant/state/note_audio/` first and deleted once the note is saved. If transcribing fails (no internet, say), the inbox still gets a note saying so, and Pakito retries the audio the next time it starts.
  - If a long turn never gets an answer (Gemini stuck, connection dropped), it's saved on its own as **"Nota recuperada"** when the conversation ends. Off-the-record conversations are never saved.
- **Questions from the dream:** the nightly dream leaves questions about you and your projects in `state/dream_questions.json`. Pakito brings one up in a normal conversation (a few hours apart, never during lock-in) and still asks them in the debrief. "¿Tienes preguntas para mí?" lists them all. Your answer is saved in your words (`pakito_heard`) for the dream to file. Answered ones never come back.
- **"Recuérdame…"**:
  - **With a time** ("el martes a las 9", "en 3 días", "mañana a las 8"): when it's due you get a toast and Pakibot pops in. Pakito keeps bringing it up in conversations until you've heard it. "Recuérdamelo mañana" snoozes it.
  - **Without a time** ("recuerda que le presté 20 a Mario"): Pakito asks you about it about 2 days later, one thing per conversation, and saves your answer.
- **"¿Qué te pedí que recordara?"** lists them. They're also kept as a checklist in `Assistant/Reminders.md`.
- **Reminders fire only while Pakito is running.** If your PC was off, it brings up what you missed the next time you talk to it.

## Dictation: hold Ctrl+Win, talk, let go
Works like Wispr Flow, anywhere you can type: WhatsApp, Canvas, VS Code, email.
- **Use:** hold **Ctrl+Win**, talk, and let go. Cleaned-up text appears where your cursor is, usually 1–2 s later.
- **What the cleanup does:**
  - punctuation, capitalization and accents;
  - removes filler ("eh", "o sea");
  - applies self-corrections ("a las 5, no, mejor a las 6" → "a las 6");
  - honors "nueva línea".
- **Language:** your Spanish/English mix stays exactly as you said it; it never translates.
- **Context:** it knows which app you're typing into, so it's casual in chats and keeps code literal in editors.
- **It stays silent for shortcuts:** Ctrl+Win+D, Ctrl+Win+arrows and so on never trigger it, and a quick tap does nothing.
- **"Escribe por mí que…"** when talking to Pakito normally: Gemini writes the message and types it into the window you're in. You press send.
- **Privacy:**
  - The audio goes to Gemini (free tier) and is dropped.
  - Logs keep only "dictated 7 s, 120 chars", never the text.
  - Off during calls.
- **Settings** (in `"voice"`):
  - `"dictation_hotkey"`;
  - `"vocabulary": ["Botana", "RKKY"]` for words it should spell right;
  - `"dictation": false` to turn it off.
- **First time:** run `run-pakito.bat check dictation` to pick the model.

## Your calendar: what's coming
Pakito reads your Google Calendar: exams (🔴), study blocks (📚), classes, work, and Canvas projects. It's the same calendar your Saturday brief and sync-brain keep up to date, so both always agree.
- **Ask:** "¿qué tengo hoy?", "¿qué viene esta semana?", "¿qué exámenes vienen?". "¿Qué hay que entregar?" includes Canvas assignments, but only when you ask.
- **Every conversation** starts with a short "coming up" list: what's left today, plus exams and projects in the next 10 days. If one is today or tomorrow, Pakito mentions it once.
- **Setup:** run `run-pakito.bat calendar` and paste each calendar's private link. In Google Calendar, go to ⚙ Settings → the calendar → Integrate calendar → **Secret address in iCal format**. For Canvas, use its Calendar Feed link.
- **The links are secret.** They're stored only in `config.local.json`, which isn't committed.
- **Refresh and offline:** Pakito refreshes every 30 min and keeps a cached copy, so it still works offline.

## "Mira esto": Pakito looks at your screen
Ask about what's in front of you: "¿qué es esto?", "explícame esta ecuación", "what's this error". Pakito takes **one** screenshot and answers out loud.
- **What it captures:**
  - the window in front (default);
  - "mira la pantalla" captures the whole monitor;
  - "mira esta parte" lets you drag a box around just the equation or paragraph (Esc cancels).
- **"Explícamelo bien / paso a paso":** Gemini gives you the quick version now, and Claude works through it properly in the background, saving the result to `Assistant/Claude/`.
- **Privacy:**
  - Only when you ask. Nothing is recorded, and nothing is logged except the window title.
  - The image goes to Gemini for that conversation and is dropped. Claude's copy is deleted the moment it finishes.
  - Free tier: Google may use what you send to improve its products.
- **Guards:**
  - It won't screenshot during a call, or when a password manager or Windows security prompt is in front.
  - Pakibot hides for the shot.
  - Agents using Pakito over MCP can't take screenshots.
- **Check it works with your model:** `run-pakito.bat check look`.

## Pakibot (the on-screen pet)
A little pixel robot that shows what Pakito is doing. Voice mode starts it automatically.
- **States:**
  - listening: big eyes, and his mouth is your live mic level, so you can see the mic hears you;
  - thinking: loading bar, plus a note while a tool runs;
  - talking: mouth follows the voice, and the speech bubble shows the live transcript;
  - focus: a real mm:ss countdown on his face and a progress bar;
  - break: naps with the break timer;
  - streak: happy when a block finishes or a Claude job is done.
- **Chest bar:** today's focused minutes toward `"daily_goal_min"` (default 120).
- **Three modes, by voice or right-click:**
  - **"Modo pop-in" / "aparece":** shows up while you talk, then fades out ~10 s later. It also pops in for news, and stays during lock-in.
  - **"Quédate":** always on screen. Shows the clock now and then.
  - **"Desaparece":** hidden until you call it back.
- **Always hidden during calls,** so it never shows in a screen share.
- **Moving it:** drag it anywhere and the position is remembered. It never takes keyboard focus and has no taskbar button.
- **Its own process:** if it crashes, voice keeps working. If Pakito quits, it closes itself. Run it alone with `python -m assistant.pet`.
- **Test it without Pakito or Gemini:** `run-pakito.bat pet` loops through every state. Drag him around; right-click → Hide to quit.
- **Cost:** about 1 ms to draw a frame, and unchanged frames are skipped. Measured on a virtual display: ~3% of one CPU core idle, ~8% while talking, near 0 when hidden.
- **Settings:** `"pet": {"enabled", "mode", "linger_sec", "daily_goal_min", "scale"}` in `config.local.json`. Change the art in `assistant/pet.py`; `pet/mockup.html` is the original sketch.

## Build jobs: Claude codes while you do something else
Say "haz que Claude agregue un settings page en RabbitHole". Claude Code works in the background and Pakito tells you when it's ready.

**Safety layers:**
1. **Its own copy.** Each job gets a separate folder (a git worktree, in `source\repos\.pakito-worktrees`) on a new `pakito/...` branch. Your checkout and branches are never touched.
2. **A reviewer.** Claude Code's auto mode has a second model check every action before it runs. It blocks anything beyond the task, or anything steered by hostile text it read; blocked actions are skipped and listed in your review. Needs Claude Code 2.1.281 or newer (`claude update`). Pakito refuses to start a build on older versions.
3. **Hard rules.** Push, switching branches, merging, resets and git config are denied outright, in every mode.
4. **You decide.** Nothing reaches your branch or GitHub until you say so.

**Review flow:**
- **"Revisa lo de RabbitHole"** opens the review in VS Code:
  - Claude's summary;
  - every command it ran and every file it edited, recorded from Claude Code itself;
  - what got blocked;
  - the full diff.
- **"Apruébalo"** merges it into your current branch, locally only. It refuses if you have uncommitted work there.
- **"Dile que también…"** sends a follow-up to the same Claude session on the same branch.
- **"Descártalo"** deletes the branch and the folder.
- **"Súbelo"** pushes, and only after it asks you and you say yes.
- **"¿Cómo va lo de X?"** gives job status. Typed: `jobs`.
- Reviews are saved in `Assistant/Builds/` in your vault, and every job is in `state/jobs.json`.

**Modo libre** (no reviewer, for local jobs you trust):
- **Unlock:** type `libre` in Pakito's console, then your passphrase. The first time, it asks you to create one; it's stored only as a salted hash in `assistant/.free_mode`. Saying it out loud doesn't count.
- **Use:** within 10 minutes, say "modo libre en RabbitHole: …". Claude Code opens in a **visible window** with no permission checks, on its own branch, with the hard rules still on. Watch it, and close the window to stop it.
- **One job per unlock.** Then review, approve or discard like any build.

Agents using Pakito over MCP can't start builds, approve, discard or push. Those stay your calls.

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
`python -m pytest tests`: 213 tests. They use a fake clock and a fake Gemini session, so nothing launches and nothing hits the network.

## Voice (Gemini Live)

```powershell
run-pakito.bat setup        # once: Gemini key, mic, browser... (or setx GEMINI_API_KEY "your-key")
run-pakito.bat
```

- **Hold Ctrl+Alt and talk, then release** (push-to-talk). The first hold opens the conversation (rising beep). After that it stays open about 60 s for follow-ups, so the next hold is instant.
- **Hold it while Pakito is talking** to cut it off. **Tap it** to hang up. Saying "that's all" / "gracias" also hangs up.
- The mic only sends while the key is held, so there's no echo problem on laptop speakers.
- **Sounds:** a high tick the moment you press means the mic is on. A lower tick when you let go means it was sent. A falling two-tone means the conversation closed.
- After each answer the console prints `⏱ 1.4s`, the time from when you let go until it starts talking. If that's often above ~2 s, try `"model": "gemini-3.1-flash-live-preview"`.
- On Windows, Pakito reads the physical keyboard 100 times a second instead of using a keyboard hook. Hooks get delayed or dropped when the program is busy or in the background; polling can't be. `run-pakito.bat keys` shows exactly when it sees DOWN/UP.
- Clicking in the console no longer freezes Pakito (QuickEdit is turned off), and it opts out of Windows background throttling.
- The same window still takes typed commands (`focus`, `d`, `stop`, `suggest`...). Run either `assistant.voice` or `assistant`, not both, because the two would keep separate focus timers.
- What you say plus the tools it calls feed the same learner, so voice requests become routine suggestions too.
- Google Search is built in, so weather and questions get answered out loud. `web_search` only opens results on screen.
- **Different key:** set `"hotkey"` in `config.local.json`, e.g. `"right ctrl"`, `"f8"`, `"ctrl+space"`. If you press a third key while holding Ctrl+Alt (Ctrl+Alt+Del, say), it's treated as a shortcut, not talking. Ctrl+Space also triggers VS Code autocomplete, and Alt+Space opens the window menu, which is why neither is the default.
- `"mode": "toggle"` brings back press-once, hands-free listening.
- **Calls:** when another app starts using the mic (Zoom, Meet in Opera GX, WhatsApp, Phone Link…), Pakito notices within about a second.
  - It hangs up if it was mid-conversation and stays off the mic until the call ends. You never hear the closing beep in your ear.
  - Pressing Ctrl+Alt during a call gives a low double-beep and nothing else.
  - It reads the same record Windows uses for the mic icon in the taskbar.
  - If some app holds the mic all the time and Pakito thinks you're always on a call, run `run-pakito.bat mic` to see its name. Then add it to `"call_ignore"`, or set `"pause_on_calls": false` to switch this off.

Settings in `assistant/config.local.json`:

```json
{ "voice": { "hotkey": "ctrl+alt", "mode": "ptt", "voice_name": "Puck", "idle_timeout_sec": 60 },
  "browser": "auto" }
```

Free tier: Google may use what you send to improve its products. Keep that in mind once email and WhatsApp tools get added.
