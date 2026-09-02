import httpx

from app.plugins.builtin.weather import WeatherCurrentTool
from app.weather.client import WeatherClient


def _mock_client(handler) -> WeatherClient:
    return WeatherClient(
        base_url="https://api.open-meteo.com", latitude=51.5, longitude=-0.1, transport=httpx.MockTransport(handler)
    )


def test_current_weather_maps_fields_and_known_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/forecast"
        assert request.url.params["latitude"] == "51.5"
        assert request.url.params["longitude"] == "-0.1"
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 18.3,
                    "relative_humidity_2m": 62,
                    "weather_code": 3,
                    "wind_speed_10m": 12.4,
                    "precipitation": 0.0,
                }
            },
        )

    client = _mock_client(handler)
    result = WeatherCurrentTool(client).run()
    assert result == {
        "temperature_c": 18.3,
        "humidity_percent": 62,
        "wind_speed_kmh": 12.4,
        "precipitation_mm": 0.0,
        "conditions": "overcast",
    }
    client.close()


def test_current_weather_falls_back_for_unknown_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"current": {"weather_code": 9999}})

    client = _mock_client(handler)
    result = WeatherCurrentTool(client).run()
    assert result["conditions"] == "weather code 9999"
    client.close()
