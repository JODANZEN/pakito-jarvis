# Pakito roadmap

## Done so far (Oct 5)
Spoken debrief (stats → what got done / still open → Examen), a vault task list (`Assistant/Tasks.md`) behind a swappable `TaskStore`.

## Next: Google login → Tasks + Calendar (+ YouTube likes)
One Google OAuth desktop client covers all three:
- **Google Tasks** replaces `LocalTasks`, so "¿qué tenemos para hoy?" works from the phone too. That's the whole point.
- **Google Calendar:** "next study block is at 10", exams in the morning brief, debrief adds tomorrow's blocks.
- **YouTube** subscriptions + likes feed the podcast picker and recommender.

Panel de Brian (the claude.ai artifact) can't be reached by local Pakito directly; it stays a planning view that Claude updates in chat.

## Private Examen
The Examen goes through Gemini's free tier right now. Option: when the Examen starts, switch to local speech-to-text (faster-whisper) and a local or paid no-training model, so reflections never leave the PC.

## Original note: to-do list that works across devices
Wanted: "¿qué tenemos para hoy?" in the morning, and the debrief says what got done and what's still missing.

Hard requirement: **the list has to live somewhere the phone can reach.** A lot of homework happens on campus, away from the PC. So Pakito must not own the list. It reads and writes a service that syncs. Options:

| Option | Phone | Setup for Pakito | Notes |
|---|---|---|---|
| Google Tasks | Tasks app + shows in Google Calendar | OAuth desktop app (like Spotify) | Already lives next to the study blocks in Calendar |
| Todoist | Best apps, natural-language dates | Just an API token | One more app to use |
| Panel de Brian (Claude artifact) | Web page, works on phone | No direct API from local code yet | It's already the daily panel; would need Claude in the loop |

Once picked: tools `tasks_today`, `add_task`, `complete_task`, and a "Tasks" section in the debrief note (done vs missing).

## Recommendations that learn your taste
Right now "pon un podcast" just offers the newest episodes you haven't played from a fixed list of creators. Next step is a small recommender:
- **Signals:** what you pick vs. skip when offered 3, what you finish vs. abandon (watch time via the extension), "ya lo escuché", and with Google login your YouTube subscriptions and likes. Optionally a Google Takeout export of watch history.
- **Model, start simple:** a score per episode = creator affinity (pick rate per creator, decayed over time) + topic match (embed titles and descriptions, compare to what you finished) + freshness + a bit of exploration, so new creators can earn a spot. That's a contextual bandit, not a black box.
- **Context:** time of day, focus vs. break, gym vs. study (e.g. Dr K at night, lectures on weekends).
- **Discovery:** suggest *new* creators similar to the ones you finish, and say why ("because you finished 3 Girard lectures").
- **Where:** runs locally on the event log Pakito already keeps, with no external service.

## Pakibot: on-screen pet (designed, not built yet)
The art and every state are in `pet/mockup.html`; open it in a browser. The look may change later.

**Modes**, switched by voice ("modo compañero" / "aparece solo cuando hablo"). Lock-in switches to companion automatically.
- *Companion:* always on screen. Sleeps when idle, and the screen shows the clock now and then.
- *Pop-in:* appears when you press Ctrl+Alt and fades out ~10 s after the conversation goes quiet.

**States**, all driven by what Pakito already tracks:
| State | When | Look |
|---|---|---|
| Idle | nothing going on | blinks, slow amber antenna, clock every few seconds |
| Listening | key held | big eyes, mouth = live mic meter (shows the mic hears you), blue antenna |
| Thinking | released → no audio yet, or a tool running | eyes up, loading bar, cycling antenna |
| Talking | Gemini audio playing | mouth moves, arm waves, transcript types out |
| Claude working | `ask_claude` job pending | thinking look + "Claude is on it (0:12)" |
| Focus | focus block running | real mm:ss countdown on the face + progress bar, red antenna |
| Break nap | break | dim screen, Zz |
| Streak | block finished without bailing | ^ ^ eyes, arms up, sparkles |
| On a call | call detector says so | hidden (so it never shows in a screen share) |

**Speech box:** what Gemini heard you say, then Pakito's reply, typed out. Long replies show the last 2–3 lines and collapse after he finishes.

**Tamagotchi layer (forgiving: never dies, never guilt-trips):**
- The chest bar is today's focus fuel (focused minutes vs. a daily goal).
- He grows with the focus level: egg at 20 min, baby at 30, teen at 45, adult at 60.
- Opening a blocked site makes him glance at it, then at you.
- "Terminé" tucks him in for the night.

**Build plan:**
- A separate small process (tkinter, no new dependencies; pixel art suits its color-key transparency).
- Always on top, click-through except a drag handle, never takes focus, remembers its position per monitor.
- It reads Pakito's state from the existing local server (`127.0.0.1:8765`, add `/pet`), so if it crashes the voice keeps working.
- `"pet": {"enabled", "mode", "scale", "corner"}` in config.

## Later
- Phone side: talk to Pakito from the S25 (Gemini Live on Android + the same hub over a private tunnel).
- Exam-prep routines for crunch weeks (book page + Canvas + lock-in).
- Calendar awareness: "next study block is at 10".
- Gmail drafts, WhatsApp prefill, Phone Link calls (with read-back confirmation).
