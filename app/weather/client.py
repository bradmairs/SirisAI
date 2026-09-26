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
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
                "temperature_unit": "celsius",
                "timezone": timezone,
                "forecast_days": days,
            },
        )
        response.raise_for_status()
        daily = response.json().get("daily", {})
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
                }
            )
        return out

    def close(self) -> None:
        self._client.close()
