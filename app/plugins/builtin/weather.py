"""Weather tool via Open-Meteo (app/weather/client.py) -- free, no API key
needed. Registered only when SIRISAI_WEATHER_LATITUDE and
SIRISAI_WEATHER_LONGITUDE are both set.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.weather.client import WeatherClient


class WeatherCurrentTool(Tool):
    name = "weather_current"
    description = "Get current weather conditions at the configured home location."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: WeatherClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.current()
