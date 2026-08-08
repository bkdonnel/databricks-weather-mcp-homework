"""
Weather-forecast MCP server.

Exposes weather-forecast tools over MCP (Model Context Protocol) so a
Databricks Agent Bricks agent can call them like any other tool:
    - get_current_weather(location)
    - get_forecast(location, days)
    - get_travel_recommendation(location, date)
    - compare_weather(locations)
    - get_historical_weather(location, start_date, end_date)

These tools are backed by Open-Meteo's free, keyless weather API (see
weather_broker.py) - no signup, no API key, no paid tier, so the server
runs with zero secrets configured. The only optional secret is a Lakebase
URL used for best-effort logging of tool calls (see _log_query below), so
the dashboard app has recent agent queries/predictions to display.

Deploy this as its own Databricks App (same app.yaml + FastMCP entrypoint
pattern documented at
https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp), separate
from the dashboard app, so an Agent Bricks agent (or any MCP client) can
register its URL as an external MCP server.

Run locally:
    python weather_mcp_server.py
"""

import logging
import os
from contextvars import ContextVar
from datetime import date, datetime, timedelta

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import weather_broker
from weather_broker import WeatherLookupError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

# Context variable to store request headers for accessing end-user identity
_request_context: ContextVar[dict] = ContextVar("request_context", default={})

# Thresholds for the derived judgment calls in get_travel_recommendation.
UMBRELLA_PRECIP_CHANCE_THRESHOLD_PCT = 40
JACKET_LOW_TEMP_THRESHOLD_F = 50
HIGH_WIND_THRESHOLD_MPH = 25
HEAVY_RAIN_THRESHOLD_IN = 0.5

mcp = FastMCP("weather-forecast")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware to capture HTTP headers containing end-user identity."""

    async def dispatch(self, request: Request, call_next):
        headers = {
            "x-forwarded-user": request.headers.get("x-forwarded-user"),
            "x-forwarded-email": request.headers.get("x-forwarded-email"),
        }
        _request_context.set(headers)
        return await call_next(request)


def _get_end_user_email() -> str | None:
    """Get the end user's email from request headers (Databricks App
    context), or None if unavailable (e.g. local dev)."""
    return _request_context.get().get("x-forwarded-user")


def _log_query(tool_name: str, location: str | None, query_date: str | None, result_summary: str, status: str) -> None:
    """
    Best-effort log of a tool call to Lakebase for the dashboard's "recent
    agent queries/predictions" view. Never raises - a missing/unreachable
    Lakebase instance must not break weather lookups, since Lakebase is
    optional infrastructure for this project, not a dependency of the
    weather tools themselves.
    """
    try:
        import lakebase

        lakebase.run_write(
            """
            INSERT INTO weather_query_log
                (user_email, tool_name, location, query_date, result_summary, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (_get_end_user_email(), tool_name, location, query_date, result_summary[:500], status),
        )
    except Exception:
        logger.warning("Failed to log query to Lakebase (non-fatal)", exc_info=True)


@mcp.tool
def get_current_weather(location: str) -> dict:
    """
    Get current conditions for a location.

    Args:
        location: City name ("Chicago"), "City, State/Country"
            ("Austin, TX"), or "lat,lon" ("41.8781,-87.6298").

    Returns:
        On success: a dict with location, temperature_f, feels_like_f,
        conditions, humidity_pct, wind_mph, wind_direction_deg,
        precipitation_in, and as_of (ISO timestamp).
        On failure: a dict with status="error" and a message - never a
        raw exception/stack trace.
    """
    try:
        result = weather_broker.get_current_conditions(location)
        _log_query("get_current_weather", location, None, f"{result['conditions']}, {result['temperature_f']}F", "success")
        return result
    except WeatherLookupError as exc:
        _log_query("get_current_weather", location, None, str(exc), "error")
        return {"status": "error", "message": str(exc)}


@mcp.tool
def get_forecast(location: str, days: int = 5) -> dict:
    """
    Get a multi-day forecast for a location.

    Args:
        location: City name, "City, State/Country", or "lat,lon".
        days: Number of days to forecast, 1-16 (default 5). Values outside
            this range are clamped.

    Returns:
        On success: a dict with location and a `days` list, each entry
        having date, conditions, high_f, low_f, precipitation_chance_pct,
        precipitation_in, and wind_mph.
        On failure: a dict with status="error" and a message.
    """
    try:
        result = weather_broker.get_daily_forecast(location, days)
        _log_query("get_forecast", location, None, f"{len(result['days'])}-day forecast", "success")
        return result
    except WeatherLookupError as exc:
        _log_query("get_forecast", location, None, str(exc), "error")
        return {"status": "error", "message": str(exc)}


@mcp.tool
def get_travel_recommendation(location: str, target_date: str | None = None) -> dict:
    """
    Derived judgment call, not a passthrough of raw forecast data: decides
    whether to bring an umbrella and/or a jacket for a location/date, and
    flags any severe-weather caution, by applying fixed thresholds to that
    day's forecast:
        - umbrella recommended if precipitation_chance_pct > 40
        - jacket recommended if the day's low temperature < 50F
        - severe weather caution if max wind > 25 mph or precipitation > 0.5 in

    Args:
        location: City name, "City, State/Country", or "lat,lon".
        target_date: Date to check, "YYYY-MM-DD" (default: today). Must be
            today or within Open-Meteo's 16-day forecast horizon - for
            dates in the past, use get_historical_weather instead.

    Returns:
        On success: a dict with location, date, forecast (the matched day's
        raw forecast entry), umbrella_recommended, jacket_recommended,
        severe_weather_caution, and reasoning (a plain-English explanation
        of which thresholds fired).
        On failure (bad location, date in the past, or date beyond the
        16-day forecast horizon): a dict with status="error" and a message.
    """
    target_date = target_date or date.today().isoformat()
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        msg = f'target_date must be "YYYY-MM-DD", got {target_date!r}'
        _log_query("get_travel_recommendation", location, target_date, msg, "error")
        return {"status": "error", "message": msg}

    today = date.today()
    if target < today:
        msg = f"{target_date} is in the past - use get_historical_weather for past dates instead."
        _log_query("get_travel_recommendation", location, target_date, msg, "error")
        return {"status": "error", "message": msg}

    days_out = (target - today).days
    if days_out >= weather_broker.MAX_FORECAST_DAYS:
        msg = (
            f"{target_date} is {days_out} days out - Open-Meteo only forecasts "
            f"{weather_broker.MAX_FORECAST_DAYS} days ahead. Try a nearer date."
        )
        _log_query("get_travel_recommendation", location, target_date, msg, "error")
        return {"status": "error", "message": msg}

    try:
        forecast = weather_broker.get_daily_forecast(location, days=days_out + 1)
    except WeatherLookupError as exc:
        _log_query("get_travel_recommendation", location, target_date, str(exc), "error")
        return {"status": "error", "message": str(exc)}

    day = weather_broker.find_forecast_day(forecast["days"], target_date)
    if day is None:
        msg = f"No forecast data returned for {target_date}."
        _log_query("get_travel_recommendation", location, target_date, msg, "error")
        return {"status": "error", "message": msg}

    precip_chance = day["precipitation_chance_pct"] or 0
    low_temp = day["low_f"]
    wind = day["wind_mph"] or 0
    precip_in = day["precipitation_in"] or 0

    umbrella = precip_chance > UMBRELLA_PRECIP_CHANCE_THRESHOLD_PCT
    jacket = low_temp is not None and low_temp < JACKET_LOW_TEMP_THRESHOLD_F
    severe = wind > HIGH_WIND_THRESHOLD_MPH or precip_in > HEAVY_RAIN_THRESHOLD_IN

    reasons = [f"Forecast for {forecast['location']} on {target_date}: {day['conditions']}, high {day['high_f']}F / low {low_temp}F."]
    reasons.append(
        f"Precipitation chance {precip_chance}% "
        f"({'>' if umbrella else '<='} {UMBRELLA_PRECIP_CHANCE_THRESHOLD_PCT}%) -> "
        f"{'bring an umbrella' if umbrella else 'umbrella not needed'}."
    )
    reasons.append(
        f"Low of {low_temp}F "
        f"({'<' if jacket else '>='} {JACKET_LOW_TEMP_THRESHOLD_F}F) -> "
        f"{'wear a jacket' if jacket else 'jacket not needed'}."
    )
    if severe:
        reasons.append(f"Caution: wind up to {wind} mph and/or {precip_in} in of precipitation expected.")

    result = {
        "location": forecast["location"],
        "date": target_date,
        "forecast": day,
        "umbrella_recommended": umbrella,
        "jacket_recommended": jacket,
        "severe_weather_caution": severe,
        "reasoning": " ".join(reasons),
    }
    _log_query("get_travel_recommendation", location, target_date, result["reasoning"], "success")
    return result


@mcp.tool
def compare_weather(locations: list[str]) -> dict:
    """
    Compare current conditions across multiple locations side by side.

    Args:
        locations: List of city names / "City, State" / "lat,lon" strings,
            e.g. ["Chicago, IL", "Austin, TX", "Miami, FL"].

    Returns:
        A dict with `results`, a list of per-location current-conditions
        dicts (see get_current_weather) in the same order as the input. A
        location that fails to resolve gets its own entry with
        status="error" and a message, instead of failing the whole
        comparison.
    """
    results = []
    for location in locations:
        try:
            results.append(weather_broker.get_current_conditions(location))
        except WeatherLookupError as exc:
            results.append({"location": location, "status": "error", "message": str(exc)})
    _log_query("compare_weather", ", ".join(locations), None, f"compared {len(locations)} locations", "success")
    return {"results": results}


@mcp.tool
def get_historical_weather(location: str, start_date: str, end_date: str | None = None) -> dict:
    """
    Look up historical daily weather for a past date or date range.

    Args:
        location: City name, "City, State/Country", or "lat,lon".
        start_date: Start of the range, "YYYY-MM-DD".
        end_date: End of the range, "YYYY-MM-DD" (default: same as
            start_date, i.e. a single day).

    Returns:
        On success: a dict with location and a `days` list, each entry
        having date, conditions, high_f, low_f, precipitation_in.
        On failure: a dict with status="error" and a message.
    """
    try:
        result = weather_broker.get_historical_weather(location, start_date, end_date)
        _log_query("get_historical_weather", location, start_date, f"{len(result['days'])} day(s) of history", "success")
        return result
    except WeatherLookupError as exc:
        _log_query("get_historical_weather", location, start_date, str(exc), "error")
        return {"status": "error", "message": str(exc)}


if __name__ == "__main__":
    if hasattr(mcp, "app") and mcp.app is not None:
        mcp.app.add_middleware(RequestContextMiddleware)

    # Databricks Apps route external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport Databricks' MCP client/gateway expects.
    # stateless_http + json_response: Databricks' AI Gateway tool-discovery
    # client calls tools/list as a single request with no prior session
    # handshake and doesn't consume a chunked text/event-stream body - the
    # default session-based SSE transport silently returns no tools to it.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
    )
