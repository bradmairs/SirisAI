# Changelog

Every change merged to `main` bumps the version (`scripts/bump_version.py`):
+0.01 for a small change, +0.1 for a significant one. Each new version on
`main` is tagged `v<version>` and released automatically. The README's
numbered milestones hold the detailed history.

## 6.48 — 2026-11-13

- Monthly review note and a weekly question about tasks open over a month
- **Monthly review** (`jobs.monthly_review`): on the 1st at the weekly hour, Siris writes `03-Resources/Monthly Reviews/<YYYY-MM> review`. It brings together:
  - active goals and their steps (done of total, next step)
  - health goals (percent, on pace or not)
  - SirisGym's best and latest lifts
  - the month's lines from the Decision Log and Lessons Learned
  - what the notes recorded

  The general model writes it as This month / Goals / Decisions and lessons / Next month. Without a model it's the facts as a list.
- **Old tasks** (`ask_about_old_tasks`, weekly): tasks written over 30 days ago with no due date. It asks about the five oldest ("done, keep, or drop"), each at most every four weeks. Answers go through brain_done. Needs vault engine 2.71, which gives each task a `since` date.

## 6.38 — 2026-11-13

- Cron alerts and editing, scheduled and one-at-a-time reboots, server timelines, workout comparisons, quicker SSH, and actions followed up
- **Cron jobs that didn't run or failed** (`app/cron/watch.py`, job `cron_watch`, every `SIRISAI_CRON_WATCH_MINUTES`, 30): a job that runs at most hourly and has no run in the cron log at its last scheduled time raises "The 03:00 backup on nas didn't run". `cron_watch_job` wraps a job so its exit code is logged (`logger -t siris-cron`), and a failed run raises an alert. Both clear by themselves.
- **Edit cron jobs** with `cron_edit_job` (add, change, delete, always asking first), or from the Cron tab's New job, Edit, Watch and Delete buttons. The schedule is checked first and described back to you.
- **Reboots at a set time**: `ssh_power` takes `at` ("reboot the NAS at 3am Sunday"). SirisAI does it then and checks the server came back. If SirisAI wasn't running at that time, it won't do it late. `ssh_power_scheduled` lists what's planned, and cancelling also clears scheduled reboots.
- **One at a time**: `ssh_reboot_needed` reboots the servers that need it after updates (or the ones named), now or at a set time. Each server has to come back before the next goes down, SirisAI's own server goes last, and it stops if one doesn't come back. When two or more servers need a reboot, the feed offers "Reboot them one at a time".
- **Server timeline** (`server_timeline`, `GET /siris/servers/{name}/timeline`): reboots and whether they came back, updates installed, failed services, full disks, not answering, disk trouble and cron runs, newest first, kept for 30 days.
- **Workouts compared** (`gym_compare`): a workout against the last one with the same name, exercise by exercise. `stalls=true` lists lifts whose estimated 1RM hasn't gone up in 3 weeks.
- **Mid-workout**: "what did I lift last time on incline press?" is answered straight away, without the model, from the last two sessions with that exercise.
- **Training in the morning brief**: on Mondays, last week's workouts, sets, records, the muscles under or over their targets, and one change. On a training day when readiness is in Recover, it suggests a lighter version: a third fewer sets and about 10% lighter. `gym_progress` says the same.
- **Faster SSH**: one connection per server is kept open for `SIRISAI_SSH_REUSE_SECONDS` (120; 0 turns it off) and reused (OpenSSH ControlMaster). When it isn't there, commands connect as before.
- **Cron tab**: the servers are asked at the same time, and the last overview is shown straight away while a fresh one loads (`?cached=true`).
- **After each update**: once per new version, `SIRISAI_RELEASE_CHECK_DELAY_SECONDS` (120) after starting, SirisAI checks every integration and runs the self-test. It pushes one alert saying what broke since the last version, or posts a quiet notice that all is well.
- **Actions followed up** (`app/core/followup.py`): any action whose own check fails (a light, lock or cover that didn't change, including protocol steps) is checked again after 30 seconds and after 2 minutes. If it still hasn't happened, the feed says "That didn't happen: turn off kitchen light".
- **What I did, by area**: `what_did_you_do` groups actions by area (Home, Servers, Containers...). Each has a details link (`/siris/activity?id=`), and the last one says whether it can be undone.
- **"Anything I need to know?"**: `attention_summary` reads the feed out, urgent first and numbered, with what can be done about each. `attention_act` dismisses or acknowledges by number, by voice. Anything else, like a reboot or an install, asks first and is only done in the app.

## 6.28 — 2026-11-13

- Friday work debrief with a CPEng gap question; memories never filed on a note they only share a date with
- **Work debrief** (`jobs.work_debrief`): every Friday at 4 pm (`SIRISAI_BRAIN_DEBRIEF_DAY`, `SIRISAI_BRAIN_DEBRIEF_HOUR_LOCAL`), a push with three questions: what you decided and why, what went wrong, what you'd do differently. It also lists your GVW projects so each answer can name its project. Answers in chat are filed by brain_learn.
- **CPEng gap question**: the same push asks about one element that CPEng Chartership's coverage table lists under "No evidence yet", the longest-unasked first. Each of the 16 elements has its own question. `SIRISAI_BRAIN_CPENG_QUESTIONS=false` turns it off.
- **Memory filing guard** (`_fits`): the model's pick of an existing note is kept only if a word of the note's title (not a date, a weekday or "session") is in the memory, or a search for the memory finds that note. Otherwise the memory waits in the inbox. "Tuesday 10 November 2026: Had a pretty good day at GVW." had been filed on "SirisGym Session (2026-11-10)".
- The nightly digest is told that titles name the thing, never a day or a session: a workout goes on Gym Training. (Vault 2.61 also folds session notes into Gym Training.)

## 6.18 — 2026-11-12

- Reads Apple Health's State of Mind (mood) from Health Auto Export, as a day's mood_valence and mood_entries

## 6.08 — 2026-11-13

- The weather outside goes to weather_current, not Home Assistant

## 6.07 — 2026-11-13

- A guessed Home Assistant entity id finds the real one instead of failing the turn

## 6.06 — 2026-11-13

- State questions (on or off, open, locked, a reading) are offered Home Assistant's get_state

## 6.05 — 2026-11-13

- The self-test runs on the app's loop, and a tool call written as text is taken as the call
- Found by the first live self-test: llama3.1 sometimes answered with just `{"name": "home_assistant_get_state", "parameters": {...}}` as text, which was shown as the answer. A reply that is exactly one call to an offered tool is now made as that call (app/llm/ollama.py, `text_tool_call`).
- Model calls from background jobs run on the app's own event loop (`scheduler.run_on_main_loop`); a loop per call failed with "Event loop is closed" from the second question on.

## 6.04 — 2026-11-13

- Faster tool turns, a nightly self-test, approvals from anywhere, and ready for a second AI machine
- 20 tools a turn instead of 32 (SIRISAI_TOOL_SELECTION_MAX): measured on the server, llama3.1:8b picked the right tool 88.7% of the time at 20 against 83.1% at 32, and 35% faster. gemma4 (thinking off) scored 78.9% at 20, so chat stays on llama3.1.
- The tool-choice eval cases are now in the Docker image (the weekly eval was failing without them).
- Nightly self-test (app/upkeep/self_test.py, SIRISAI_SELF_TEST_HOUR_LOCAL=4): asks about the weather, a light and the day, checks against the weather service, Home Assistant and the clock; wrong answers go to the attention feed. Its turns may only use READ tools.
- Approval pushes get an "Open in SirisOS" button (SIRISAI_SIRISOS_PUBLIC_URL), which works away from home; Approve/Decline still post to SIRISAI_PUBLIC_BASE_URL.
- SIRISAI_EMBEDDING_BASE_URL, and a "Two AI machines" guide in deploy/README.md.

## 5.94 — 2026-11-12

- Return windows on things you bought: read from the receipt, with a last-chance alert

## 5.93 — 2026-11-12

- Reliability fixes, faster replies, a second AI machine, breakage alerts, notifications where you are, Hey Siris, and OneNote removed
- Pushes fit ntfy (long briefings were refused for a week), a shortened one opens SirisOS, and a push that fails lands in the feed.
- Presence holds its state through the gateway's nightly restart; one unreadable to-do list no longer hides the rest.
- Faster: the fast role can be the same model with thinking off (SIRISAI_FAST_LLM_THINK, SIRISAI_OLLAMA_THINK).
- A second AI machine: SIRISAI_OLLAMA_FALLBACK_BASE_URLS, tried when one is down or erroring.
- Breakage watch: something broken is pushed within the hour with its likely fix, daily while it lasts, and when it's fixed.
- Notifications go where you are: held overnight (SIRISAI_QUIET_HOURS), spoken on the HUD at home, reminders and urgent ones always pushed.
- "Hey Siris" on the HUD: listens for the wake phrase, transcribed locally; everything else is dropped.
- Removed: the OneNote import and the handwriting review routes.

## 5.83 — 2026-11-12

- **Brain watch:** every 15 minutes, checks the Obsidian sync, server inventory and secrets backup heartbeats (from the vault's `.siris-heartbeats.json`), the dream and upkeep, and unpushed vault commits. Pushes an alert when one goes quiet, again daily while it lasts, and "back to normal" when it clears.
- **One git at a time:** the brain client takes the vault's shared `.git/siris-git.lock` (the engine's Obsidian sync uses the same file), so two pulls no longer collide.
- **Batched commits:** vault changes are committed together every `SIRISAI_BRAIN_COMMIT_INTERVAL_SECONDS` (900) instead of once per write.
- **Decisions:** `brain_record_decision` tool and capture `kind: "decision"` write to the Decision Log.
- **Stalled projects:** the weekly job asks carry on, pause or archive (each project at most every 4 weeks); `brain_project_status` acts on the answer.
- **People:** birthdays this week in the briefing; a weekly nudge with birthdays and people not heard about in 90 days.
- **On this day** in the morning briefing.
- **Voice memos:** `POST /siris/brain/capture` takes `audio` (a data URL), transcribed by the configured speech-to-text and kept in Attachments.
- **Weekly tidy:** the biggest notes get a model-written "Current state" block and their older updates move to a history note; weak automatic links are pruned. The rebuild readiness table is refreshed in the Home Lab Rebuild Guide.

## 5.73 — 2026-11-11

- Cron details and adding servers, reboots that are checked through, and asking about SirisGym workouts
- **Cron details**: the Cron tab (and `cron_list_jobs`) now shows each server's name, time zone and uptime, and for every job the schedule in words ("Every weekday at 08:00"), the next run in the server's time, when it last ran (from the cron log, last 3 days), who it runs as and where it's from. System jobs in `/etc/crontab` and `/etc/cron.d` are listed too (read only). `GET /siris/cron_jobs/overview`.
- **Add a cron server** from the Cron tab: `POST /siris/cron_jobs/hosts` tests the connection first, then saves `SIRISAI_CRON_HOSTS` through the Settings store and starts using it at once (no restart). A server that refuses SirisAI's key gets the public key to add to `authorized_keys`. Remove with `DELETE /siris/cron_jobs/hosts/{user@host}`. Both take the PIN.
- **Reboot fixed**: an immediate reboot ran as a background job that logind could kill when the SSH session closed, so nothing happened, or the server dropping the connection was reported as a failure. Now `systemctl reboot` runs in the foreground, a dropped connection counts as started, and every reboot or shutdown is followed up by the server's boot id: "nas rebooted and is back (1m 40s)", or an alert when it didn't reboot or hasn't come back in 10 minutes (with Wake-on-LAN). The feed's Reboot button no longer closes as done when nothing happened.
- **Ask about SirisGym workouts**: new `gym_sessions` tool for "what did I do at the gym on Monday", "show me my last leg day", "what did I bench last week": every exercise and set, records, notes and the Watch's effort. Reads SirisOS with the service key (SirisOS 8.41), falling back to the workouts kept here. Private health data, like `gym_progress`.

## 5.63 — 2026-11-11

- OneNote import off unless switched on; presence waits 10 minutes before everyone's out; away mode from SirisOS

## 5.53 — 2026-11-08

- Web search always returns at least five results; the model used to ask for one and get a footnote

## 5.52 — 2026-11-08

- Web search is offered on every turn, so questions about anything after 2023 get looked up

## 5.51 — 2026-11-08

- Stop tracking things you removed on purpose

## 5.41 — 2026-11-08

- Receipts show the receipt itself (the invoice PDF, a photo, or the email), pictures put back in the email, and new projects from SirisOS

## 5.31 — 2026-11-07

- Receipts found in email are kept, with the email and attachments, and linked from Things; everything in the Gmail Receipts label is read

## 5.21 — 2026-11-07

- Receipts in email become Things: read-only, once a day and on request, with scripts/setup_gmail.sh to connect Gmail

## 5.11 — 2026-11-07

- Protocols can be turned off, and their notifications too

## 5.01 — 2026-11-07

- Health Auto Export grouped by minute: syncs split mid-hour add up, switching from hours never doubles, per-second workout heart rate thinned to 5 s

## 5.0 — 2026-11-07

- A speed trial that proves (or disproves) the speed work, quick commands for scenes, media and the thermostat that also learn, undo by voice, a weekly reliability report, things and warranties, people and birthdays, a house watch for leaks and doors, news in the morning brief, and roles for who's speaking
- Speed trial: POST/GET/DELETE /siris/timings/trial; verdict and fitted SIRISAI_OLLAMA_WARM_HOURS in the feed when it ends
- Quick commands: scenes, media, volume, thermostat, "undo that", and phrases learned after three identical model answers
- Undo: undo_last and "undo that" for home actions, reminders, automations, scenes, documents and settings; never unlocks or disarms
- GET /siris/reliability and a Sunday report (SIRISAI_RELIABILITY_HOUR_LOCAL)
- Things: /siris/things, from receipts too; warranty warnings (SIRISAI_WARRANTY_WARN_DAYS)
- People: /siris/people, birthdays from the calendar, gift ideas, last seen, keep in touch (SIRISAI_PEOPLE_REMIND_DAYS)
- House watch: leaks, warm fridge/freezer, doors left open, low batteries, monthly house health (SIRISAI_HOUSE_WATCH_SECONDS and limits)
- News: SIRISAI_NEWS_FEEDS, SIRISAI_NEWS_TOPICS, SIRISAI_PODCAST_FEEDS; GET /siris/news; in the spoken brief
- Who's speaking: SIRISAI_VOICE_ROLES, SIRISAI_VOICE_UNKNOWN_AS_GUEST
- Fixed: 4.6's document and speed-trial attention items used a kind the hub contract doesn't allow

## 4.9 — 2026-11-07

- Stricter CPEng evidence (re-checked, Brad can reject a note), a coverage table, and handwriting review routes for SirisOS
- The nightly CPEng step only counts notes showing Brad's own engineering on a real job, re-reads the existing list and removes lines that no longer count; `brain_not_cpeng_evidence` rejects one for good.
- *CPEng Chartership* gets a coverage table: notes per element, and the elements with none yet.
- `/siris/brain/handwriting` (queue), `/handwriting/image` (the page drawn from OneNote) and `/handwriting/check`, for SirisOS 7.6's handwriting review. Needs the vault engine 2.28.

## 4.8 — 2026-11-06

- Recently updated notes by folder (GET /siris/brain/recent), for SirisOS's Work page

## 4.7 — 2026-11-06

- Topic summaries, CPEng competency drafts, a past-projects search in chat, and date-only OneNote titles named after their series
- Weekly: a "what you've learned" summary on each topic page in Siris (Culverts, Stormwater and Hydrology…), and a first draft of each CPEng competency element with enough evidence, on *CPEng Competency Drafts*.
- Chat: `brain_past_projects` answers "have I dealt with this before?" from project and topic pages. Needs the vault engine 2.08.

## 4.6 — 2026-11-06

- Faster replies (model kept warm, cache-friendly prompts, instant home commands, per-turn timings), a scheduler where one stuck job can't hold up the rest, and life admin: safe automations from speech, a document inbox, bills from email, travel mode, and conversation summaries
- Speed: SIRISAI_OLLAMA_KEEP_ALIVE (30m) and SIRISAI_OLLAMA_WARM_HOURS (6-23); the context block moved next to the newest message so Ollama can reuse its cache; tools chosen while context is gathered; one embedding per message; quick home commands (SIRISAI_QUICK_COMMANDS); GET /siris/timings
- Steadier: every background job on its own loop, never overlapping itself; GET /siris/scheduler/jobs; pooled TTS connection; daily PRAGMA optimize and WAL checkpoint
- Automations: plain-English draft in the approval; never unlock, open or disarm by themselves
- Pushes describe the request in words; "Approve in SirisOS" for PIN approvals (SIRISAI_SIRISOS_PUBLIC_URL)
- Conversations: titles and summaries by the local model; GET /siris/conversations/search with dates
- Document inbox: POST /siris/documents, filed in Siris, reminders before due dates; documents_search
- Bills from email: GET /siris/bills, raised SIRISAI_BILLS_WARN_DAYS ahead; bills_upcoming
- Travel mode: GET /siris/travel, weather, packing and house lists, "Away while I'm gone"; travel_upcoming

## 4.5 — 2026-11-05

- Updates check through git when the repo is private, and make update installs the updater

## 4.49 — 2026-11-05

- Edit Siris notes and memories from SirisOS, and the phones that count as home (Dom's iPhone added)

## 4.39 — 2026-11-05

- Set-by-set heart rate from each Watch reading, the settle time that teaches SirisGym's rest timer, and cardio, niggles and same-weight fitness in training answers

## 4.29 — 2026-11-05

- SirisGym's coach: plain advice after a workout from the set-by-set analysis (rest longer, reps falling away, room to add weight), and GET /siris/health/gym/coach, at most two changes for the week from training, sleep, recovery and protein. GET /siris/brain/inbox/count for a light badge.

## 4.28 — 2026-11-05

- Set-by-set effort: links SirisGym sessions to the Watch workout and lines heart rate up with each set

## 4.18 — 2026-11-05

- SirisGYM is now SirisGym; gym_progress says when today's a rest day and when the next workout is

## 4.17 — 2026-11-01

- Health data you can trust: split syncs add up, faster stalled/today-missing alerts, per-source inspector, dry runs, why readiness was low, a monthly report

## 4.16 — 2026-11-01

- Ask about your training: gym_progress answers from SirisGYM's summary (lift trends, what to lift next, muscles under target, records, deloads)

## 4.06 — 2026-11-01

- SirisGYM sessions count in strain (not twice with the Watch); recent mornings' readiness for the deload check

## 4.05 — 2026-11-01

- SI units everywhere: energy said and shown in kJ, car range in km

## 4.04 — 2026-11-01

- Health Auto Export: today arrives again (Date Range Default, not Previous 7 Days), and hourly-grouped or per-stage sleep is read as one night
- The setup check flags an export sent today that stops at yesterday, and names Date Range Default
- Bedtime ignores an event that started today and runs over tomorrow morning

## 4.03 — 2026-10-31

- Strain, a weekly sleep report, a Health Auto Export setup check, the morning brief spoken, an updater heartbeat, and a pre-push check

## 3.93 — 2026-10-31

- health_history: your health over any stretch of days against the month before, for questions about the past

## 3.83 — 2026-10-31

- Network nodes carry UniFi's name flag and first-seen date, and the network says whether SirisAI alerts on new devices

## 3.82 — 2026-10-31

- Health trends (this week against your month) and an early heads-up a day before the getting-sick warning

## 3.72 — 2026-10-31

- Container updates show live progress, and anything that falls over afterwards is rolled back
- Any feed action that takes over 20 seconds answers "still working" and reports with a notice, instead of the phone giving up ("Load failed")

## 3.62 — 2026-10-31

- Readiness from the night (kept once the morning is over) and an energy score for the day

## 3.52 — 2026-10-31

- New-device alerts only for devices new to UniFi; container updates run in the background

## 3.51 — 2026-10-30

- Health in one place, with insights: every Apple Health metric, workouts and sleep in SirisAI; readiness for chat and voice; backfill, illness warning, sleep insights, bedtime, goals, quick logs, privacy, export
- One health store for SirisAI and SirisOS: every metric Apple Health gives, sleep times and stages, workouts and voice-logged notes, per person, with source priority so two imports of the same day don't double up.
- Readiness, sleep score, training load, vitals, a smoothed weight trend and a getting-sick check, worked out in SirisAI and shared at `/siris/hub/v1/health/overview`, so "how ready am I?" works in chat and by voice.
- Backfill years of history from the Health app's `export.zip` (phone and watch steps not doubled).
- "Health data hasn't arrived" in the feed when Health Auto Export stops for 36 hours.
- Insights: a getting-sick warning with **Block my evening**, what your sleep goes with (late meetings, workouts, caffeine, drinks, a warm room, late TV), tonight's bedtime with a wind-down nudge and protocol, and goals like "run 1,000 km this year".
- Log water, caffeine, drinks, mood, symptoms, supplements and meals by voice; a meal photo gets a rough estimate from the local vision model.
- Health answers never go to a cloud model (`SIRISAI_HEALTH_PRIVATE`); CSV export, a printable doctor summary, and delete by date range or source.

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
