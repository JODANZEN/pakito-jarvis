# Pakito roadmap

## Next: to-do list that works across devices
Wanted: "¿qué tenemos para hoy?" in the morning, and the debrief says what got done and what's still missing.

Hard requirement: **the list has to live somewhere the phone can reach.** A lot of homework happens on campus, away from the PC. So Pakito must not own the list. It reads and writes a service that syncs. Options:

| Option | Phone | Setup for Pakito | Notes |
|---|---|---|---|
| Google Tasks | Tasks app + shows in Google Calendar | OAuth desktop app (like Spotify) | Already lives next to the study blocks in Calendar |
| Todoist | Best apps, natural-language dates | Just an API token | One more app to use |
| Panel de Brian (Claude artifact) | Web page, works on phone | No direct API from local code yet | It's already the daily panel; would need Claude in the loop |

Once picked: tools `tasks_today`, `add_task`, `complete_task`, and a "Tasks" section in the debrief note (done vs missing).

## Later
- Phone side: talk to Pakito from the S25 (Gemini Live on Android + the same hub over a private tunnel).
- Exam-prep routines for crunch weeks (book page + Canvas + lock-in).
- Calendar awareness: "next study block is at 10".
- Gmail drafts, WhatsApp prefill, Phone Link calls (with read-back confirmation).
