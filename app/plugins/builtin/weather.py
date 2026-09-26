"""Weather tool via Open-Meteo (app/weather/client.py) -- free, no API key
needed. Registered only when SIRISAI_WEATHER_LATITUDE and
SIRISAI_WEATHER_LONGITUDE are both set.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.weather.client import WeatherClient


class WeatherForecastTool(Tool):
    name = "weather_forecast"
    description = (
        "Get the weather forecast for the next few days at the home location: conditions, high/low, and "
        "chance of rain per day (today first). Use for 'will it rain tomorrow', 'do I need an umbrella', "
        "'what's the weekend like'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"days": {"type": "integer", "description": "How many days to include, 1-7. Defaults to 3."}},
        "required": [],
    }

    def __init__(self, client: WeatherClient, timezone: str = "auto") -> None:
        self._client = client
        self._timezone = timezone

    def run(self, days: int = 3, **kwargs: Any) -> Any:
        return self._client.forecast(days=days, timezone=self._timezone)


class WeatherCurrentTool(Tool):
    name = "weather_current"
    description = "Get current weather conditions at the configured home location."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: WeatherClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.current()
