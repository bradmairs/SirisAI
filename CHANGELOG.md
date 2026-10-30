# Changelog

Every change merged to `main` bumps the version (`scripts/bump_version.py`):
+0.01 for a small change, +0.1 for a significant one. Each new version on
`main` is tagged `v<version>` and released automatically. The README's
numbered milestones hold the detailed history.

## 3.41 — 2026-10-30

- Home and safety: a weekly energy report, away mode, a camera digest, a weekly audit digest, and settings history with undo
- Energy report (Mondays, `energy_report`): kWh and cost against the week before, the biggest users, and anything left on overnight, from Home Assistant's energy and power sensors.
- Away mode (`SIRISAI_AWAY_MODE`): when everyone leaves, offer (or run) the away protocol; undo it when someone's back, keeping doors locked and the garage shut.
- Camera digest (12pm, 6pm, 10pm; `camera_summary`): one message saying what Frigate saw, with couriers and known faces, instead of an alert per event.
- Weekly audit digest (Sundays, `audit_digest`): what SirisAI did on its own, held for your OK or was refused, plus wrong PINs and settings changes.
- Settings history: every change on the Settings page is listed with **Undo**, which puts back what it replaced (needs the PIN, like saving).

## 3.31 — 2026-10-30

- Homelab upkeep: container updates with roll-back, server backup checks and tests, new network devices, an internet outage log, expiring certificates and domains
- Container updates: a daily check for newer images, **Update all** in the feed, each container recreated with its own settings and rolled back automatically if it doesn't come up healthy (`docker_check_updates`, `docker_update_containers`).
- Server backups (`SIRISAI_SERVER_BACKUPS`: ZFS snapshots or backup folders): overdue ones go to the feed, and on the 1st of each month a random file is read back out of each.
- New devices on the network, from UniFi, with **Name it** and **Block** (`network_name_device` from chat).
- An internet outage log: a probe every minute, "The internet is down" while it is, speed tests every 6 hours, and a weekly summary for your ISP (`internet_outages`).
- Certificates (including HTTPS on each server) and domains (RDAP) expiring within 14 days go to the feed (`expiry_check`).
- Updating containers and blocking devices need the approval PIN when one is set. The new settings are on the Settings page.

## 3.21 — 2026-10-30

- OneNote tidied: clearer titles, deleted pages stay deleted, action items, CPEng evidence, handwriting checks
- New OneNote pages get their section in front of generic titles, and dates as YYYY-MM-DD.
- The nightly upkeep adds tasks from recent emails, OneNote pages and photos, and lists work notes that evidence the CPEng competencies on *CPEng Chartership*.
- The weekly note check starts with a handwritten page. Needs the vault engine 1.86.

## 3.11 — 2026-10-30

- Network alerts (block by MAC), power use from Home Assistant, and Health Auto Export's JSON for Apple Health

## 3.01 — 2026-10-29

- Waking a server or computer (`ssh_wake`) now asks first, like reboot and shutdown already did. A spoken "yes" is enough to wake something; reboot and shutdown still have to be confirmed in the app. The attention feed's Wake button counts as the confirmation.
- Any computer on the LAN can be woken by name, not just SSH servers: list it in `SIRISAI_SSH_WAKE_MACS` as `name=MAC` or `name=MAC@address` (also editable in Settings). With an address, SirisAI watches for it to come up by whether it answers on a common port.
- Voice: a reply counts as a question when its last sentence asks one, or the one before a sign-off does ("Shall I turn them off? Just say the word."), so the HUD and satellites listen for the answer more often.
- The HUD now starts listening by itself after SirisAI asks something, instead of waiting for a tap, and gives up quietly after 8 seconds of silence. It keeps one microphone audio context from the first tap, so this also works on iPhone.

## 2.91 — 2026-10-25

- Protocols can be made and changed from SirisOS: the hub lists every protocol with its steps and what a step can do, and saves or deletes them, allowing only device, notification and guest-mode steps (anything else is kept as made in chat).

## 2.9 — 2026-10-25

- Sudo checks over SSH mean passwordless now: an account allowed a command only with a password (pi-1's apt-get) is told so before anything runs, instead of the background update failing. A failed update says why, with the sudoers line to add.

## 2.89 — 2026-10-25

- Presence says when you got home (or left): the hub's presence has `since`, and the first check after setup is marked as a baseline, not an arrival. For SirisOS 4.9's welcome home.

## 2.88 — 2026-10-25

- OneNote pages are split across the notes they cover, like captures: the sort now sees long pages whole (Second Brain 1.76), splits only when the pieces hold most of the note, and an edit to a page that was split goes to the inbox to be sorted the same way.

## 2.87 — 2026-10-25

- Siris splits a capture about several things across the notes it's about: each piece, in your own words, becomes a dated update on its note (or a new note), and the original is archived. Needs Second Brain 1.74.

## 2.77 — 2026-10-24

- Live network: real WAN and device rates on a Cloud Gateway Ultra
- **WAN:** it's now read from the gateway's `wan1` (plus `wan2`) record, which holds a steady rate. `stat/health`'s `wan` figure, used before, reads 0 between the controller's refreshes on a UCG Ultra, so the house view showed no internet traffic most of the time. It stays as the fallback for older gateways.
- **Devices:** on a UCG Ultra the gateway, switches and access points don't report their own `rx/tx_bytes-r`; those figures are spread over port tables. Each device now carries the traffic flowing through it, which is what a link in the house view draws: the gateway carries the WAN rate, and a switch or access point carries its clients plus the devices below it. Devices that do report their own rate keep it.
- Checked live against the UCG Ultra: WAN is steady, and each access point and the switch match their clients.

## 2.76 — 2026-10-24

- Live network for SirisOS's house view: UniFi gateway, APs, switches and clients with their traffic

## 2.66 — 2026-10-24

- A tidier brain: calendar events don't become notes; memories from SirisOS's wrap-up

## 2.56 — 2026-10-23

- Home controls for SirisOS: lights, garage, locks and scenes
  - `GET /siris/hub/v1/controls`; `SIRISAI_HUB_CONTROLS` picks which entities show.
  - `POST /siris/hub/v1/controls/{entity}`: garage doors and unlocking need `confirmed`; guest mode hides both.
  - `POST /siris/hub/v1/scenes/{id}/run` runs Siris and Home Assistant scenes.

## 2.46 — 2026-10-23

- Safer approvals and a Settings page: an approval PIN, approvals that expire, and settings from the web UI
- `SIRISAI_APPROVAL_PIN`: rebooting or shutting down servers, installing updates, SSH commands that change things and saving settings need the PIN, wherever they're approved (chat, the feed, the API). Five wrong PINs lock them for 10 minutes and raise an urgent alert.
- Phone notification buttons offer only Decline for those actions, and a spoken "yes" never approves them (`ssh_install_updates` and `ssh_run` join `ssh_power`).
- Actions held for approval expire after `SIRISAI_APPROVAL_EXPIRY_MINUTES` (15). Approving one late returns 410 and closes it.
- The web UI's Settings tab edits server, approval and daily-job settings. Saved values win over `.env`, survive restarts, and mostly apply at once. Secrets and the PIN stay in `.env`.
- Hub contract: optional `pin` on `AttentionActRequest` (additive).

## 2.36 — 2026-10-23

- Looking after the servers: Wake-on-LAN, a server watch, OS updates, disk health and sudo permissions
- `ssh_wake`: Wake-on-LAN with MACs learned automatically, sent via a relay server, Home Assistant or directly, then a check that it came back.
- The server watch (every 15 minutes) raises full disks, memory pressure, failed services (Restart) and servers that stop answering (Wake), and clears them when fixed.
- Daily OS update and disk health checks: "2 updates waiting (1 security)" with Install updates, "needs a reboot" with Reboot, SMART/NVMe/ZFS/RAID problems.
- `ssh_server_permissions` says which sudo permissions are missing and the exact rule to add. Works with sudo-rs (Ubuntu 25.10): every recommended rule names an exact command, since sudo-rs takes no wildcards.
- `/siris/servers` routes.

## 2.26 — 2026-10-23

- Health by day for up to 400 days, for SirisOS's year view

## 2.25 — 2026-10-23

- Project list and email by subject for SirisOS's Projects screen

## 2.24 — 2026-10-22

- Email into Siris, photos read, real capture titles, duplicate merging and work project pages
- Label an email "Siris" in Gmail and it's in Siris' inbox the next morning (read-only IMAP with an app password; `SIRISAI_GMAIL_*`).
- The nightly upkeep reads inbox photos with the vision model, gives cut-off capture titles a real name, and refreshes the GVW project pages.
- Chat can merge duplicate notes (`brain_merge_notes`) or mark them different (`brain_notes_distinct`). Needs the vault engine 1.61.

## 2.14 — 2026-10-22

- The hub for SirisOS's day: presence, reminders, emails with meeting attendees, upkeep, health by day, vault tasks, edited triage replies
- Calendar events carry `attendees`, from the event's attendee and organiser addresses.
- New hub endpoints (all additive):
  - `GET /presence`: home or away, from the last presence check.
  - `GET /reminders` and `POST /reminders/{id}/done`.
  - `GET /email/with?address=`: the latest emails from the people in a meeting.
  - `GET /upkeep`: backups, and whether an update is waiting.
  - `GET /health/days`: health metrics day by day.
- `POST /attention/{id}/act` takes an optional `body`, so a triage reply can be edited in SirisOS before it's sent.
- `GET /siris/brain/tasks` lists every open task in the vault. It needs vault engine 1.41.
- Health takes a new `running_km` metric (also accepted as `run_km`) from the iPhone shortcut.

## 2.04 — 2026-10-22

- Reboot and shut down one, several or all servers, always asking first
- `ssh_power`: reboot, shutdown (now or after a delay) or cancel a pending one, on a server, a list, or all.
- Always needs a tap on Allow/Approve: never a spoken "yes", never on SirisAI's own initiative.
- Non-root accounts need passwordless sudo for just the power commands (deploy/README.md has the rule); the reply says so when it's missing.
- `SIRISAI_SSH_SELF_SERVER` makes "all" do SirisAI's own machine last; `SIRISAI_SSH_ALLOW_POWER=false` turns it off.

## 1.94 — 2026-10-22

- SSH to servers on your network: health checks and commands, with changes asking first
- `SIRISAI_SSH_HOSTS` names the servers (blank uses the cron hosts); same SSH key as the cron overview.
- `ssh_server_health` reports load, memory, disks and failed services with warnings; `ssh_run` runs a command.
- Look-only commands run straight away; anything else (or anything touching secrets) asks first. `SIRISAI_SSH_ALLOW_CHANGES=false` makes it look-only.
- Server output is treated as untrusted text.

## 1.84 — 2026-10-19

- SirisAI's web UI, HUD and mind map now use SirisOS's Liquid Glass look
- A shared `/siris-glass.css` with SirisOS's tokens, wallpaper and glass material (SirisOS ADR 106).
- Glass header with the dock's active-tab lens, glass views and cards, tinted pill buttons, accent and glass chat bubbles, a pill composer.
- An opt-in light theme, as in SirisOS, from the settings row.

## 1.74 — 2026-10-18

- OneNote sign-in steps name the right container for the deploy/ stack (sirisai)

## 1.73 — 2026-10-18

- OneNote into Siris: nightly import of OneNote pages, handwriting transcribed by the local vision model
- New pages go to Siris' inbox to be sorted; edited pages add only what changed. Set up in README milestone 136.

## 1.63 — 2026-10-17

- The second brain is called Siris everywhere you see it (vault 1.21); SirisAI keeps its name

## 1.62 — 2026-10-17

- Mark tasks and projects done
- `POST /siris/brain/done` ticks an overdue task in its note, or marks a project finished so its deadline and leftover tasks leave insights. Chat can do it too (`brain_mark_done`). Needs the vault engine 1.3.

## 1.52 — 2026-10-17

- More useful: calc notes, CPD and evidence capture, phone approvals, morning triage
- `engineering_calc_note`: runs a SirisOS calculator, checks the standards through SirisHydro with citations, ties it to a project and saves the write-up to the Second Brain.
- Evening capture offers CPD-looking calendar events as CPD entries and a day of engineering work as chartership evidence; `career_log_cpd`, `career_drafts` and a CSV export for Engineers Australia.
- Held actions get Approve and Decline buttons on the phone that act on the attention item itself.
- Morning triage drafts replies to the emails that need you (saved as drafts; Send asks first) and flags calendar clashes.

## 1.42 — 2026-10-17

- Upkeep: SirisAI looks after itself
- Nightly database backups (consistent, gzipped, newest 14 kept, optional copy to a NAS folder) and a monthly restore drill that proves the newest one opens.
- Daily housekeeping: prunes old audit/metric/feed rows, watches disk space, database size and token use, and warns in the feed. Container logs capped in `deploy/`.
- Self-update with rollback: SirisAI offers new releases (or applies them with `SIRISAI_UPDATE_MODE=auto`); `deploy/updater.sh` (`make auto-update`) applies them and rolls back if the new version isn't healthy or picks tools worse.
- Files its own GitHub issues (asking first by default) for problems it can't fix, with secrets blanked.
- Model upgrades gated by the tool-choice evals; a better candidate is offered, a worse one left alone.
- `/siris/upkeep` routes and the `upkeep_status` tool.

## 1.32 — 2026-10-17

- The learning loop: SirisAI learns from what you undo, correct, accept and ignore
- Outcomes are recorded for feed answers, ignored suggestions, autonomous actions you undo or keep, chat corrections and failed checks.
- An action it takes on its own that you keep undoing is held for your OK next time. Approvals earn it back.
- Habit suggestions you mostly turn down stop buzzing your phone and wait in the feed.
- Nightly reflection (3am) turns corrections into eval cases and proposes facts to remember, which you approve or decline in the feed.
- Chat says "I checked afterwards and..." when a change didn't take.
- A weekly goal review puts each goal's progress and next step in the feed.
- `/siris/learning` routes, the `what_have_you_learned` tool, and a once-a-day job runner other upkeep jobs build on.

## 1.22 — 2026-10-17

- Photo capture: a photo sent to /siris/brain/capture is saved in the vault's Attachments/ and embedded in the note

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
