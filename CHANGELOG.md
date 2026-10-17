# Changelog

Every change merged to `main` bumps the version (`scripts/bump_version.py`):
+0.01 for a small change, +0.1 for a significant one. Each new version on
`main` is tagged `v<version>` and released automatically. The README's
numbered milestones hold the detailed history.

## 1.21 — 2026-10-17

- Hub calendar range: every event between two dates, for SirisOS's week view and CPD logging
- Hub forecast: `GET /siris/hub/v1/forecast?days=7`, the week's daily forecast for SirisOS's week view

## 1.2 — 2026-10-17

- The second brain checks on itself, answers questions with sources, and takes Claude chats (vault 1.2)
- Nightly summary at low priority (notes learned in the last day, what the dream found, recall score). A high-priority alert when nothing has been learned for 3 days or recall drops by more than 5 points.
- Nightly recall check: the vault's 30 known questions, scored and recorded for the mind map.
- `POST /siris/brain/ask`: a short answer from the brain, with its sources and "as of" dates. Chat context shows "as of" too.
- Claude chats: capture with `"kind": "chat"`; the upkeep pulls out what's worth keeping and keeps the chat in 03-Resources/Claude Chats.
- A weekly "check these notes" push, plus `brain_mark_reviewed`, `GET /siris/brain/review` and `POST /siris/brain/reviewed`. Setting: `SIRISAI_BRAIN_REVIEW_NOTES` (default 3).

## 1.1 — 2026-10-16

- Delete and restore Second Brain notes: routes, plus brain_delete_note (asks first) and brain_restore_note
- `POST /siris/brain/delete` `{title, permanent?}`, `/restore`, `/purge` and `GET /siris/brain/deleted`. They run the vault engine's new commands (Siris-Second-Brain 1.1, PR #7) and are committed and pushed like every vault write. A deleted note moves to the vault's `.deleted/` folder, with links to it removed; restore brings it back with its connections.
- Siris can delete a note when asked ("forget the note about X"). It always asks Allow / Cancel first, and it never deletes permanently; `brain_restore_note` undoes it.

## 1.0 — 2026-10-11

The first numbered release: everything up to README milestone 128, plus
the calendar, career and second-brain changes merged since (#70–#73).

- **Assistant core.** Chat and streaming over a local model, with a planner, a fast-model router and tool selection that matches by meaning. There's long-term memory per user, explainable autonomy (an audit trail, undo and a trust ledger), a taint guard for untrusted content, and confirmation tiers for risky actions.
- **Voice.** Streaming spoken replies with barge-in and follow-ups; room satellites with wake words; speaker recognition by voice; voice confirmation for safe actions.
- **Home.** Home Assistant control, scenes, protocols (Lockdown, Away, Guest, Night) with undo, and guest mode. Also presence, geofence reminders, habit-based automation suggestions, energy-aware scheduling, and cameras with a vision model and intruder response.
- **Homelab.** Health checks, self-healing containers, a security briefing (certificates, SSH, IPS, Trivy, backups) and cron across hosts. Covers Docker, UniFi, Synology, Plex, the *arr apps and speed tests.
- **Daily life.** A morning briefing, calendars (CalDAV, Gmail and iCloud feeds, Home Assistant), email triage, health data from your phone, the car, parcels, reminders, routines, goals, and career CPD and chartership tracking.
- **Second Brain.** Reads and grows the Siris-Second-Brain vault: search, an inbox, auto-linking, meeting notes, nightly upkeep, a weekly review and a mind map.
- **SirisOS.** A versioned hub contract (`/siris/hub/v1`) tested on both sides, a live attention feed with approvals, and the engineering library (standards, calculators, SirisHydro) as tools.
- **Running it.** A web UI and HUD, a PWA, Siri Shortcuts and HTTPS. `deploy/` runs SirisAI and SirisOS together with Docker Compose.
