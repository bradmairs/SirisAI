"""Thin client for Open-Meteo (https://open-meteo.com/en/docs) -- free,
no API key or account required, just a latitude/longitude.
"""

from datetime import datetime

import httpx

_WMO_WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


class WeatherClient:
    def __init__(
        self,
        base_url: str,
        latitude: float,
        longitude: float,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._latitude = latitude
        self._longitude = longitude
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def current(self) -> dict:
        response = self._client.get(
            "/v1/forecast",
            params={
                "latitude": self._latitude,
                "longitude": self._longitude,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
            },
        )
        response.raise_for_status()
        current = response.json().get("current", {})
        code = current.get("weather_code")
        return {
            "temperature_c": current.get("temperature_2m"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "precipitation_mm": current.get("precipitation"),
            "conditions": _WMO_WEATHER_CODES.get(code, f"weather code {code}"),
        }

    def forecast(self, days: int = 3, timezone: str = "auto") -> list[dict]:
        """Daily outlook for the next `days` days (today first)."""
        days = max(1, min(int(days), 7))
        response = self._client.get(
            "/v1/forecast",
            params={
                "latitude": self._latitude,
                "longitude": self._longitude,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,uv_index_max",
                # 6.88: hourly UV, for when sun protection is needed (UV 3 or more)
                "hourly": "uv_index",
                "temperature_unit": "celsius",
                "timezone": timezone,
                "forecast_days": days,
            },
        )
        response.raise_for_status()
        body = response.json()
        daily = body.get("daily", {})
        windows = _uv_windows(body.get("hourly") or {})
        out = []
        for i, day in enumerate(daily.get("time", [])):
            code = (daily.get("weather_code") or [None] * (i + 1))[i]
            out.append(
                {
                    "date": day,
                    "weekday": datetime.fromisoformat(day).strftime("%A"),
                    "conditions": _WMO_WEATHER_CODES.get(code, f"weather code {code}"),
                    "high_c": (daily.get("temperature_2m_max") or [None] * (i + 1))[i],
                    "low_c": (daily.get("temperature_2m_min") or [None] * (i + 1))[i],
                    "rain_chance_percent": (daily.get("precipitation_probability_max") or [None] * (i + 1))[i],
                    "rain_mm": (daily.get("precipitation_sum") or [None] * (i + 1))[i],
                    "uv_index_max": (daily.get("uv_index_max") or [None] * (i + 1))[i],
                    **windows.get(day, {}),
                }
            )
        return out

    def hourly(self, days: int = 4, past_days: int = 0, timezone: str = "auto") -> dict:
        """7.3: hour by hour for the next `days` (and `past_days` back, for the rain that's fallen), with sunrise and sunset."""
        response = self._client.get(
            "/v1/forecast",
            params={
                "latitude": self._latitude,
                "longitude": self._longitude,
                "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,uv_index,wind_speed_10m",
                "daily": "sunrise,sunset",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "timezone": timezone,
                "forecast_days": max(1, min(int(days), 7)),
                "past_days": max(0, min(int(past_days), 7)),
            },
        )
        response.raise_for_status()
        body = response.json()
        h = body.get("hourly") or {}

        def at(key: str, i: int):
            vals = h.get(key) or []
            return vals[i] if i < len(vals) else None

        hours = [{"time": t, "temperature_c": at("temperature_2m", i), "rain_chance_percent": at("precipitation_probability", i),
                  "rain_mm": at("precipitation", i), "conditions": _WMO_WEATHER_CODES.get(at("weather_code", i)),
                  "uv_index": at("uv_index", i), "wind_kmh": at("wind_speed_10m", i)} for i, t in enumerate(h.get("time") or [])]
        d = body.get("daily") or {}
        sun = [{"date": day, "sunrise": (d.get("sunrise") or [None] * (i + 1))[i], "sunset": (d.get("sunset") or [None] * (i + 1))[i]}
               for i, day in enumerate(d.get("time") or [])]
        return {"latitude": self._latitude, "longitude": self._longitude, "hours": hours, "sun": sun}

    def close(self) -> None:
        self._client.close()


SUN_PROTECTION_UV = 3  # the Cancer Council's line: sun protection needed at UV 3 and above


def _uv_windows(hourly: dict) -> dict[str, dict[str, str]]:
    """Each day's first and last hour at UV 3 or more: {"2026-11-19": {"sun_protection_from": "09:00", "sun_protection_to": "16:00"}}."""
    out: dict[str, dict[str, str]] = {}
    for stamp, uv in zip(hourly.get("time") or [], hourly.get("uv_index") or []):
        if uv is None or uv < SUN_PROTECTION_UV:
            continue
        day, hour = stamp[:10], stamp[11:16]
        span = out.setdefault(day, {"sun_protection_from": hour, "sun_protection_to": hour})
        span["sun_protection_to"] = f"{int(hour[:2]) + 1:02d}:00" if int(hour[:2]) < 23 else "23:59"
    return out
