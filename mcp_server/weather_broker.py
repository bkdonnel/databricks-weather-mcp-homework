"""
Open-Meteo weather adapter backing the weather-forecast MCP server.

Thin wrapper around Open-Meteo's free, keyless REST APIs:
    - Geocoding API (geocoding-api.open-meteo.com) resolves a "City, ST" /
      "City, Country" string (or a "lat,lon" pair) to coordinates.
    - Forecast API (api.open-meteo.com) returns current conditions and a
      multi-day daily forecast.
    - Archive API (archive-api.open-meteo.com) returns historical daily
      weather for past dates.

No API key is required for any of these - Open-Meteo's non-commercial free
tier (~10,000 calls/day) needs no signup, so there are no secrets to manage
here (compare to a broker that reads keys via WorkspaceClient().secrets -
this module intentionally has none of that plumbing).

All HTTP calls and JSON parsing live in this module so weather_mcp_server.py's
@mcp.tool functions stay thin passthroughs with no raw `requests` calls.
"""

import os
from typing import Any

import requests

_GEOCODE_URL = os.environ.get(
    "OPEN_METEO_GEOCODE_URL", "https://geocoding-api.open-meteo.com/v1/search"
)
_FORECAST_URL = os.environ.get(
    "OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast"
)
_ARCHIVE_URL = os.environ.get(
    "OPEN_METEO_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive"
)
_TIMEOUT = 15
MAX_FORECAST_DAYS = 16

# WMO weather interpretation codes -> human-readable conditions.
# https://open-meteo.com/en/docs (see "WMO Weather interpretation codes")
_WEATHER_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherLookupError(ValueError):
    """Raised for a bad/unresolvable location, an out-of-range date, or an
    upstream Open-Meteo failure. Caught in weather_mcp_server.py and turned
    into a clean {"status": "error", ...} dict instead of a stack trace."""


def describe_weather_code(code: int | None) -> str:
    """Translate a WMO weather code into a short human-readable string."""
    if code is None:
        return "Unknown"
    return _WEATHER_CODES.get(code, f"Unknown (code {code})")


def geocode(location: str) -> dict[str, Any]:
    """
    Resolve a free-text location ("Chicago", "Austin, TX", "Paris, France")
    or a "lat,lon" pair to coordinates + a display name.

    Raises:
        WeatherLookupError: if the location is empty, can't be resolved by
            Open-Meteo's geocoder, or the geocoding request fails.
    """
    location = (location or "").strip()
    if not location:
        raise WeatherLookupError("location is required")

    latlon = _parse_latlon(location)
    if latlon is not None:
        lat, lon = latlon
        return {"latitude": lat, "longitude": lon, "resolved_name": location, "country": None}

    try:
        resp = requests.get(
            _GEOCODE_URL,
            params={"name": location, "count": 1, "language": "en", "format": "json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise WeatherLookupError(f"Geocoding lookup failed for {location!r}: {exc}") from exc

    results = resp.json().get("results") or []
    if not results:
        raise WeatherLookupError(
            f'Could not resolve location "{location}". Try a more specific name '
            '(e.g. "Springfield, IL") or pass "lat,lon" directly.'
        )

    top = results[0]
    name_parts = [top.get("name"), top.get("admin1"), top.get("country")]
    return {
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "resolved_name": ", ".join(p for p in name_parts if p),
        "country": top.get("country"),
    }


def get_current_conditions(location: str) -> dict[str, Any]:
    """Fetch current temperature, conditions, humidity, and wind for a location."""
    place = geocode(location)
    params = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,weather_code,wind_speed_10m,wind_direction_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
    }
    data = _get(_FORECAST_URL, params)
    current = data.get("current", {})
    return {
        "location": place["resolved_name"],
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "as_of": current.get("time"),
        "temperature_f": current.get("temperature_2m"),
        "feels_like_f": current.get("apparent_temperature"),
        "conditions": describe_weather_code(current.get("weather_code")),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_mph": current.get("wind_speed_10m"),
        "wind_direction_deg": current.get("wind_direction_10m"),
        "precipitation_in": current.get("precipitation"),
    }


def get_daily_forecast(location: str, days: int = 5) -> dict[str, Any]:
    """Fetch a multi-day forecast (highs/lows, precip chance, conditions), up
    to Open-Meteo's 16-day forecast horizon."""
    days = max(1, min(int(days), MAX_FORECAST_DAYS))
    place = geocode(location)
    params = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": days,
    }
    data = _get(_FORECAST_URL, params)
    daily = data.get("daily", {})
    dates = daily.get("time", [])

    day_entries = [
        {
            "date": day_str,
            "conditions": describe_weather_code(_at(daily.get("weather_code"), i)),
            "high_f": _at(daily.get("temperature_2m_max"), i),
            "low_f": _at(daily.get("temperature_2m_min"), i),
            "precipitation_chance_pct": _at(daily.get("precipitation_probability_max"), i),
            "precipitation_in": _at(daily.get("precipitation_sum"), i),
            "wind_mph": _at(daily.get("wind_speed_10m_max"), i),
        }
        for i, day_str in enumerate(dates)
    ]

    return {"location": place["resolved_name"], "days": day_entries}


def get_historical_weather(location: str, start_date: str, end_date: str | None = None) -> dict[str, Any]:
    """Fetch historical daily weather (high/low/precipitation) for a past
    date or date range, e.g. start_date="2026-07-01", end_date="2026-07-07"."""
    end_date = end_date or start_date
    place = geocode(location)
    params = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "auto",
    }
    data = _get(_ARCHIVE_URL, params)
    daily = data.get("daily", {})
    dates = daily.get("time", [])

    day_entries = [
        {
            "date": day_str,
            "conditions": describe_weather_code(_at(daily.get("weather_code"), i)),
            "high_f": _at(daily.get("temperature_2m_max"), i),
            "low_f": _at(daily.get("temperature_2m_min"), i),
            "precipitation_in": _at(daily.get("precipitation_sum"), i),
        }
        for i, day_str in enumerate(dates)
    ]

    if not day_entries:
        raise WeatherLookupError(
            f"No historical data returned for {place['resolved_name']} "
            f"between {start_date} and {end_date}."
        )

    return {"location": place["resolved_name"], "days": day_entries}


def find_forecast_day(forecast_days: list[dict], target_date: str) -> dict | None:
    """Find the entry in a get_daily_forecast() `days` list matching target_date (YYYY-MM-DD)."""
    return next((d for d in forecast_days if d["date"] == target_date), None)


def _parse_latlon(location: str) -> tuple[float, float] | None:
    """Return (lat, lon) if `location` is a "lat,lon" pair, else None."""
    parts = [p.strip() for p in location.split(",")]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _get(url: str, params: dict) -> dict:
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise WeatherLookupError(f"Weather API request failed: {exc}") from exc
    return resp.json()


def _at(values: list | None, index: int):
    if not values or index >= len(values):
        return None
    return values[index]
