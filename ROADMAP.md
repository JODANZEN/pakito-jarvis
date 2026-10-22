# Pakito roadmap

## Built (as of Oct 17)
Voice (Gemini Live), focus trainer + lock-in, routines, Spotify, podcasts ("pon un podcast" offers 3 new episodes),
debrief + Examen, vault tasks, Claude asks + background builds with review/approve, MCP server, Pakibot pet,
"mira esto" screenshots, calendar reader (iCal; run `run-pakito.bat calendar` to connect), dictation (Ctrl+Win),
call detection, "toma nota" → inbox, "recuérdame" reminders.

## Next: Pakito off the PC
The PC-only parts (Spotify, apps, lock-in) stay on the PC; what has to follow you moves to things that sync.
1. **Google Tasks + Calendar** (one Google OAuth desktop client, like Spotify). Tasks and reminders reach the
   phone and fire even when the PC is off. "¿Qué tenemos para hoy?" works from campus. Notes already sync (OneDrive).
2. ✅ **Mac + PC sharing one memory** (Oct 22): see "Two computers" in assistant/README.md.
3. **Voice from the S25:** a small web app on the phone that talks to the PC's hub over a private tunnel
   (Tailscale). Cheap; works while the PC is on.
4. **Always-on brain** (only if 1–3 aren't enough): the hub runs on a small cloud server and the PC becomes a
   device it controls when online.

## Maybe later
- Podcast picker that learns your taste: pick vs. skip, finish vs. abandon, creator affinity + topic match +
  a bit of exploration, all on the local event log. Today it just offers the newest unplayed episodes.
- Gmail drafts, WhatsApp prefill, Phone Link calls (with read-back confirmation).
- Email polish for dictation ("hazlo formal", "mejora esto" on selected text).

## Dropped
- Private Examen with local speech recognition: not a worry for now.
- Canvas exam-prep routines: ASU locked the Canvas API token. Studying happens in ChatGPT.
