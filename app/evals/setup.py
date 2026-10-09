"""A SirisAI with every integration configured (to placeholder addresses) so
every tool is registered -- what the tool-choice evals score against. Nothing
here contacts those addresses: clients connect lazily, and the evals never
run tools."""

from typing import Any

from app.core.config import Settings

FULLY_CONFIGURED: dict[str, Any] = dict(
    home_assistant_base_url="http://ha.invalid", home_assistant_token="t", plex_base_url="http://plex.invalid", plex_token="t",
    docker_base_url="http://docker.invalid", unifi_base_url="http://unifi.invalid", unifi_api_key="k", presence_device_names="phone",
    weather_latitude=1.0, weather_longitude=1.0, caldav_url="http://cal.invalid", ntfy_topic="t", hermes_base_url="http://hermes.invalid",
    tautulli_base_url="http://t.invalid", tautulli_api_key="k", radarr_base_url="http://r.invalid", radarr_api_key="k",
    sonarr_base_url="http://s.invalid", sonarr_api_key="k", bazarr_base_url="http://b.invalid", bazarr_api_key="k",
    listenarr_base_url="http://l.invalid", listenarr_api_key="k", cleanuparr_base_url="http://c.invalid", cleanuparr_api_key="k",
    obsidian_api_key="k", frigate_base_url="http://f.invalid", vision_model="v", synology_base_url="http://n.invalid",
    synology_username="u", synology_password="p", cron_hosts="root@pi.invalid", tts_provider="openai_compatible",
    email_imap_host="imap.invalid", email_username="u", email_password="p", car_battery_entity_id="sensor.car_battery",
    car_climate_entity_id="climate.car", seventeentrack_api_key="k", energy_cheap_windows="00:00-06:00",
    brain_path="/brain.invalid", sirisos_url="http://sirisos.invalid", sirisos_service_key="k",
)


def fully_configured_settings(**overrides: Any) -> Settings:
    return Settings(**{**FULLY_CONFIGURED, **overrides})
