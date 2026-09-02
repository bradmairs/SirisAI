from app.core.config import Settings


def test_blank_weather_lat_long_env_vars_are_treated_as_unset(monkeypatch) -> None:
    # Regression test: a .env file with `SIRISAI_WEATHER_LATITUDE=` (present
    # but blank, as .env.example ships it) used to raise a pydantic
    # ValidationError instead of falling back to the float | None default,
    # because pydantic-settings passes the literal "" through rather than
    # omitting the field.
    monkeypatch.setenv("SIRISAI_WEATHER_LATITUDE", "")
    monkeypatch.setenv("SIRISAI_WEATHER_LONGITUDE", "")

    settings = Settings(_env_file=None)

    assert settings.weather_latitude is None
    assert settings.weather_longitude is None


def test_weather_lat_long_env_vars_still_parse_when_set(monkeypatch) -> None:
    monkeypatch.setenv("SIRISAI_WEATHER_LATITUDE", "51.5")
    monkeypatch.setenv("SIRISAI_WEATHER_LONGITUDE", "-0.1")

    settings = Settings(_env_file=None)

    assert settings.weather_latitude == 51.5
    assert settings.weather_longitude == -0.1
