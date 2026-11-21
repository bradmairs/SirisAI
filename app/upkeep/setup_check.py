"""What's not set up (7.49): "what am I missing?", GET /siris/upkeep/setup.

SirisAI has many optional parts. This lists, for each, whether it's set up,
what it would unlock, and the setting to change; then settings that are
set but look wrong (a Plex name Tautulli doesn't know, a door camera Frigate
doesn't have, a hidden tool that doesn't exist...). Read-only: nothing is
changed, and secrets are never shown, only whether they're set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)


def _on(v: Any) -> bool:
    return bool(v) and v != "none"


# (feature, is it set up?, what it unlocks, the settings to set)
FEATURES: list[tuple[str, Any, str, str]] = [
    ("A local AI model", lambda s: _on(s.llm_provider), "Everything that needs understanding: chat, voice, the brief", "SIRISAI_LLM_PROVIDER=ollama, SIRISAI_OLLAMA_MODEL"),
    ("Hearing (speech-to-text)", lambda s: _on(s.stt_provider), "Talking to SirisAI on the HUD, SirisOS and satellites", "SIRISAI_STT_PROVIDER, SIRISAI_STT_BASE_URL"),
    ("A voice (text-to-speech)", lambda s: _on(s.tts_provider), "Spoken replies, announcements, the door", "SIRISAI_TTS_PROVIDER, SIRISAI_TTS_BASE_URL, SIRISAI_TTS_VOICE"),
    ("Meaning-based search", lambda s: _on(s.embedding_provider), "Memories and tools found by meaning, not just words", "SIRISAI_EMBEDDING_PROVIDER=ollama"),
    ("Home Assistant", lambda s: bool(s.home_assistant_base_url and s.home_assistant_token), "Lights, heating, locks, music, shopping lists, presence", "SIRISAI_HOME_ASSISTANT_BASE_URL, SIRISAI_HOME_ASSISTANT_TOKEN"),
    ("Notifications", lambda s: bool(s.ntfy_topic), "Pushes to your phone, approvals, 'tell me when'", "SIRISAI_NTFY_TOPIC"),
    ("Calendar", lambda s: bool(s.caldav_url), "Your day in the brief, departures, training plans", "SIRISAI_CALDAV_URL"),
    ("Email", lambda s: bool(s.email_imap_host), "Bills, receipts and replies from email", "SIRISAI_EMAIL_IMAP_HOST, SIRISAI_EMAIL_USERNAME"),
    ("Plex", lambda s: bool(s.plex_base_url and s.plex_token), "What's playing, what to watch, the watchlist", "SIRISAI_PLEX_BASE_URL, SIRISAI_PLEX_TOKEN"),
    ("Tautulli", lambda s: bool(s.tautulli_base_url and s.tautulli_api_key), "Your watch history in the brief, next seasons", "SIRISAI_TAUTULLI_BASE_URL, SIRISAI_TAUTULLI_API_KEY"),
    ("Sonarr", lambda s: bool(s.sonarr_base_url and s.sonarr_api_key), "Getting the next season with one tap", "SIRISAI_SONARR_BASE_URL, SIRISAI_SONARR_API_KEY"),
    ("Radarr", lambda s: bool(s.radarr_base_url and s.radarr_api_key), "Adding films", "SIRISAI_RADARR_BASE_URL, SIRISAI_RADARR_API_KEY"),
    ("Cameras (Frigate)", lambda s: bool(s.frigate_base_url), "Intruder alerts, who's at the door", "SIRISAI_FRIGATE_BASE_URL"),
    ("Seeing (a vision model)", lambda s: bool(s.vision_model), "Describing camera snapshots and photos", "SIRISAI_VISION_MODEL"),
    ("Parcel tracking", lambda s: bool(s.seventeentrack_api_key), "Parcel updates, 'probably your parcel' at the door", "SIRISAI_SEVENTEENTRACK_API_KEY"),
    ("Second brain", lambda s: bool(s.brain_path or s.obsidian_api_key), "Notes, the weekly review, 'what do my notes say'", "SIRISAI_BRAIN_PATH or SIRISAI_OBSIDIAN_API_KEY"),
    ("Servers over SSH", lambda s: bool(s.ssh_hosts), "Server health, disks, backups, cron", "SIRISAI_SSH_HOSTS"),
    ("A hosted model (optional)", lambda s: s.cloud_llm_when in ("fallback", "hard") and bool(s.cloud_llm_base_url and s.cloud_llm_model), "Hard questions, and answers when the local model is down", "SIRISAI_CLOUD_LLM_WHEN, _BASE_URL, _MODEL, _API_KEY"),
    ("Everyone else's accounts", lambda s: bool(s.people_accounts), "'My history' meaning whoever is speaking", "SIRISAI_PEOPLE_ACCOUNTS, SIRISAI_OWNER_USER_IDS"),
]


def _problems(siris: "SirisAI") -> list[str]:
    s = siris.settings
    out: list[str] = []
    if s.cloud_llm_when in ("fallback", "hard") and not (s.cloud_llm_base_url and s.cloud_llm_model):
        out.append(f"The hosted model is set to '{s.cloud_llm_when}' but SIRISAI_CLOUD_LLM_BASE_URL or _MODEL is missing, so it's never used.")
    if s.cloud_llm_when in ("fallback", "hard") and s.cloud_llm_base_url and not s.cloud_llm_api_key:
        out.append("The hosted model has no SIRISAI_CLOUD_LLM_API_KEY (fine for a local server, not for a hosted one).")
    known = {t.name for t in siris.tools.list()}
    missing = sorted(n for n in siris.hidden_tool_names() if n not in known)
    if missing:
        out.append(f"Hidden tools that don't exist (renamed or not set up): {', '.join(missing)}.")
    client = getattr(siris, "_tautulli_client", None)
    if client is not None:
        from app.core import identity

        names = identity.accounts(s).get("plex", [])
        if names:
            try:
                if not client.find_user_ids(names):
                    out.append(f"None of your Plex names ({', '.join(names)}) are Plex users Tautulli knows, so 'my history' falls back to a name search.")
            except Exception:
                pass
    frigate = getattr(siris, "_frigate_client", None)
    if frigate is not None and s.door_cameras:
        try:
            cameras = {c.get("name") if isinstance(c, dict) else c for c in frigate.list_cameras()}
            wrong = [c.strip() for c in s.door_cameras.split(",") if c.strip() and c.strip() not in cameras]
            if wrong:
                out.append(f"Door cameras Frigate doesn't have: {', '.join(wrong)}.")
        except Exception:
            pass
    if s.people_accounts and not s.owner_user_ids:
        try:
            enrolled = {v["user_id"] for v in siris.voiceprints.list_voiceprints()}
        except Exception:
            enrolled = set()
        if enrolled:
            out.append("Other people's accounts are set but SIRISAI_OWNER_USER_IDS isn't, so your own voice may be treated as someone else's. "
                       f"Enrolled voices: {', '.join(sorted(enrolled))}.")
    if s.music_follow_me and getattr(siris, "_home_assistant_client", None) is None:
        out.append("Music follows you is on, but Home Assistant isn't set up.")
    if (s.llm_provider == "ollama") and s.ollama_num_ctx and s.ollama_num_ctx < 4096:
        out.append(f"The model's context is only {s.ollama_num_ctx} tokens: tool turns won't fit (SIRISAI_OLLAMA_NUM_CTX, 8192 suggested).")
    return out


def report(siris: "SirisAI") -> dict[str, Any]:
    s = siris.settings
    features = []
    for name, check, unlocks, setting in FEATURES:
        try:
            on = bool(check(s))
        except Exception:
            on = False
        features.append({"feature": name, "set_up": on, "unlocks": unlocks, "settings": setting})
    missing = [f for f in features if not f["set_up"]]
    problems = _problems(siris)
    summary = (f"{len(features) - len(missing)} of {len(features)} parts are set up." +
               (f" Not yet: {', '.join(f['feature'] for f in missing[:6])}{' and more' if len(missing) > 6 else ''}." if missing else "") +
               (f" {len(problems)} setting(s) look wrong." if problems else ""))
    return {"summary": summary, "features": features, "not_set_up": missing, "problems": problems}
