# Pakito roadmap

## Built (as of Nov 22)
Voice (Gemini Live, long sessions, retries when the connection hiccups), focus trainer + lock-in, routines, Spotify,
podcasts, debrief + Examen, vault tasks, Claude asks + background builds with review/approve, MCP server, Pakibot,
"mira esto" screenshots, calendar reader (iCal), dictation (Ctrl+Win), call detection, "toma nota" → inbox,
"recuérdame" reminders, browser tabs (list / switch / close / read), Mac + PC sharing one memory, setup wizard.

**Nov 22:**
- **Claude can use his notes.** Questions to Claude can read and write the Second Brain (following its CLAUDE.md).
  When Claude gets blocked from something else (Google Calendar, a site), Pakito says so and "dale permiso" allows
  it from then on and asks again. Voice never allows a shell.
- **Follow-ups** (`state/followups.json`): what he said he'd get to doesn't get lost. Blocked or failed Claude
  questions land there by themselves; the daily dream reads his conversations and adds what he said he wanted
  done, with a plan; Pakito brings one up every few hours and hands it to Claude ("pásaselo a Claude").

## Next: Tony Stark mode (plan from the Nov 22 research)
Rule of thumb: **named actions first, vision last.** "Maximize Spotify on screen 2" is a Windows call that always
works in milliseconds; screenshot-and-click agents are slower and still take several times more steps than needed,
so they're only a fallback for apps we can't name.

**Phase 0: foundations**
- Check the Gemini Live model's shutdown date and move to its replacement (re-test tools after switching).
- Safety tiers for actions: instant (focus, move, media), say-what-you'll-do (type, open links, terminals),
  confirm first (close unsaved, delete, send, submit). "Pakito, para" stops everything. Everything is logged.

**Phase 1: desktop control by voice (Windows first, Mac after)**
- Windows: open / close / minimize / maximize / focus, move to screen 1 or 2, snap left/right.
- Layouts: "modo trabajo" opens and arranges a set of apps across the monitors (per computer, since each has its
  own screens).
- Claude Code in any folder, not only repos ("abre Claude en Ercilia Works"): folder aliases + Windows Terminal.
- Copy/paste between apps: "copia esto", "pégalo en Claude".

**Phase 2: hands-free**: ✅ Nov 23 with "Hey Jarvis", then "Pakito" trained on his own voice (`run-pakito train`:
5 min of recording, a small network on openWakeWord's speech features, trained locally). Later: echo
cancellation if he wants speakers + interrupting it.
- Local wake word "Pakito" (openWakeWord or livekit-wakeword; train on his own voice, tune the threshold).
  Nothing goes to Google until it hears its name.
- Conversation stays open hands-free until "gracias Pakito" / silence; a ring buffer keeps the words right after
  "Pakito".
- Echo cancellation (WebRTC APM) so speakers work; headphones until then.

**Phase 3: tablet tutor (Galaxy Tab S11 Ultra)**: ✅ built Nov 23 over adb (his PC has no Miracast, so no
Second Screen). "Mira mi tablet" (Pakito tutors) / "mándaselo a ChatGPT" (saved + copied; he pastes it in his chat).
- Tablet as a second Windows screen (Samsung Second Screen, Win+K; the PC needs Miracast Wi-Fi: test first).
  Fallback: `adb exec-out screencap` over wireless debugging.
- "Pakito, take a look": screenshot the tablet's screen → clipboard → focus the ChatGPT/Claude tab → paste with
  "check my work, hint only". He presses Enter (auto-sending and reading answers breaks their terms).
  Gemini only hears the command: the image never costs Pakito tokens.

**Phase 4: gestures (experimental, optional)**: ✅ first version Nov 23 (Windows): point to move, pinch to
click/drag, two fingers to scroll, "mouse a la pantalla 2" by voice. Next: tune on his real webcam.
- Webcam hand mouse (MediaPipe + One Euro filter) in "clutch" mode (moves only while holding a pose), screen
  chosen by voice. Point/pinch to pick a text box, then "Pakito, escribe esto aquí".
- Arms get tired fast: never the main way to point. Project Gameface (head pointer) as a no-hands option.

**Phase 5: packaging**: ✅ Nov 23: `update-pakito` (pull + install + health check), `check all`, optional extras in
setup (hands-free, hand control, tablet). Not doing uv for now: the setup scripts already install in one step.
- uv project + lockfile + one bootstrap script per OS, so the Denver PC and the Mac install in one command.
- Optional extras (wake word, gestures) so a voice-only install stays light.
- Per-computer settings (screens, mic, layouts, folders) kept out of the shared OneDrive config.

**For Jose (not Pakito):** Claude Code plugins worth trying: `security-guidance`, `code-review`, `feature-dev`,
`pyright-lsp` / `typescript-lsp` / `csharp-lsp`, `context7`, `playwright`, and `unity-mcp` for NextLab.

## Earlier ideas still open
- Google Tasks + Calendar sync so reminders reach the phone when the PC is off.
- Voice from the phone (small web app over Tailscale).
- Podcast picker that learns his taste.
- Gmail drafts, WhatsApp prefill (with read-back confirmation).

## Dropped
- Private Examen with local speech recognition: not a worry for now.
- Canvas exam-prep routines: ASU locked the Canvas API token. Studying happens in ChatGPT.
- Projector whiteboard: too faint in daylight for the webcam (Nov 22).
