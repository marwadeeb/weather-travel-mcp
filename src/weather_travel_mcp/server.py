"""Weather & Travel MCP server built with FastMCP.

Exposes exactly three tools, one per category:

* Data tool          -> get_weather_forecast  (read-only lookup, Open-Meteo API)
* Action tool        -> save_trip_plan        (writes a trip to the local trips file)
* Orchestration tool -> plan_trip             (forecast -> pick best day -> save)

plus one read-only resource, ``trips://saved``, listing everything saved so far.

Run it standalone with:  uv run python -m weather_travel_mcp.server
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

# Log to stderr: stdout is reserved for the MCP stdio protocol.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="    [mcp-server] %(message)s",
)
log = logging.getLogger("weather_travel_mcp")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_S = 10.0
MAX_FORECAST_DAYS = 16  # Open-Meteo's limit

DEFAULT_TRIPS_FILE = Path(__file__).resolve().parents[2] / "data" / "trips.json"

# WMO weather interpretation codes -> short text (subset used by Open-Meteo).
WMO_CODES: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


# --------------------------------------------------------------------------- #
# Typed outputs
# --------------------------------------------------------------------------- #
class DailyForecast(BaseModel):
    date: str = Field(description="ISO date (YYYY-MM-DD), local to the city")
    conditions: str = Field(description="Human-readable weather summary")
    temp_max_c: float
    temp_min_c: float
    precipitation_probability_pct: int
    precipitation_mm: float
    wind_max_kmh: float


class Forecast(BaseModel):
    city: str = Field(description="Resolved city name")
    country: str
    latitude: float
    longitude: float
    timezone: str
    days: list[DailyForecast]


class SavedTrip(BaseModel):
    id: str = Field(description="Unique id of the saved trip")
    city: str
    date: str = Field(description="ISO date of the trip (YYYY-MM-DD)")
    summary: str
    notes: str
    saved_at: str = Field(description="UTC timestamp when the trip was saved")


class RankedDay(BaseModel):
    date: str
    score: float = Field(description="0-100, higher is better travel weather")
    conditions: str


class TripPlan(BaseModel):
    best_day: DailyForecast
    reason: str = Field(description="Why this day was chosen")
    ranking: list[RankedDay] = Field(description="All candidate days, best first")
    saved_trip: SavedTrip


# --------------------------------------------------------------------------- #
# Internal helpers (shared by the tools; the orchestration tool reuses these)
# --------------------------------------------------------------------------- #
def _trips_file() -> Path:
    return Path(os.environ.get("TRIPS_FILE", DEFAULT_TRIPS_FILE))


def _load_trips() -> list[dict[str, Any]]:
    path = _trips_file()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"Trips file {path} is corrupted and cannot be read: {exc}") from exc


def _write_trips(trips: list[dict[str, Any]]) -> None:
    path = _trips_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trips, indent=2), encoding="utf-8")


async def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET a JSON document, turning network problems into a clear ToolError."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException as exc:
        raise ToolError("Weather service timed out. Please try again in a moment.") from exc
    except httpx.HTTPStatusError as exc:
        raise ToolError(
            f"Weather service returned HTTP {exc.response.status_code}; it may be unavailable."
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Weather service is unreachable ({type(exc).__name__}).") from exc


async def _fetch_forecast(city: str, days: int) -> Forecast:
    city = city.strip()
    if not city:
        raise ToolError("City name must not be empty.")
    if not 1 <= days <= MAX_FORECAST_DAYS:
        raise ToolError(f"days must be between 1 and {MAX_FORECAST_DAYS}, got {days}.")

    geo = await _get_json(GEOCODING_URL, {"name": city, "count": 1, "language": "en"})
    matches = geo.get("results") or []
    if not matches:
        raise ToolError(f"Unknown city '{city}'. Check the spelling or try a nearby larger city.")
    place = matches[0]

    data = await _get_json(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": ",".join([
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max",
            ]),
            "timezone": "auto",
            "forecast_days": days,
        },
    )
    d = data["daily"]
    daily = [
        DailyForecast(
            date=d["time"][i],
            conditions=WMO_CODES.get(d["weather_code"][i], f"WMO code {d['weather_code'][i]}"),
            temp_max_c=d["temperature_2m_max"][i],
            temp_min_c=d["temperature_2m_min"][i],
            precipitation_probability_pct=d["precipitation_probability_max"][i] or 0,
            precipitation_mm=d["precipitation_sum"][i] or 0.0,
            wind_max_kmh=d["wind_speed_10m_max"][i],
        )
        for i in range(len(d["time"]))
    ]
    return Forecast(
        city=place["name"],
        country=place.get("country", ""),
        latitude=place["latitude"],
        longitude=place["longitude"],
        timezone=data.get("timezone", place.get("timezone", "")),
        days=daily,
    )


def _store_trip(city: str, trip_date: str, summary: str, notes: str) -> SavedTrip:
    city = city.strip()
    if not city:
        raise ToolError("City name must not be empty.")
    try:
        parsed = date.fromisoformat(trip_date)
    except ValueError as exc:
        raise ToolError(f"Invalid date '{trip_date}'. Use ISO format YYYY-MM-DD.") from exc
    if parsed < date.today():
        raise ToolError(f"Cannot save a trip in the past ({trip_date}).")

    trips = _load_trips()
    for t in trips:
        if t["city"].lower() == city.lower() and t["date"] == parsed.isoformat():
            raise ToolError(
                f"A trip to {city} on {parsed.isoformat()} is already saved (id {t['id']})."
            )

    trip = SavedTrip(
        id=uuid.uuid4().hex[:8],
        city=city,
        date=parsed.isoformat(),
        summary=summary.strip(),
        notes=notes.strip(),
        saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    trips.append(trip.model_dump())
    _write_trips(trips)
    return trip


def _score_day(day: DailyForecast, preferred_temp_c: float) -> float:
    """Heuristic 0-100 'nice travel day' score."""
    score = 100.0
    score -= day.precipitation_probability_pct * 0.4          # up to -40
    score -= min(day.precipitation_mm, 20) * 1.5               # up to -30
    avg_temp = (day.temp_max_c + day.temp_min_c) / 2
    score -= min(abs(avg_temp - preferred_temp_c) * 2, 30)     # up to -30
    score -= max(day.wind_max_kmh - 25, 0) * 0.5               # strong wind
    if "Thunderstorm" in day.conditions or "Heavy" in day.conditions:
        score -= 20
    return round(max(score, 0.0), 1)


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
mcp = FastMCP(
    name="weather-travel",
    instructions=(
        "Weather and travel planning tools. Use get_weather_forecast to look up weather, "
        "save_trip_plan to save a trip the user has already decided on, and plan_trip when "
        "the user wants you to pick the best day to visit a city and save it."
    ),
)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_weather_forecast(
    city: Annotated[str, Field(description="City name, e.g. 'Beirut' or 'Paris'")],
    days: Annotated[
        int, Field(ge=1, le=MAX_FORECAST_DAYS, description="Number of days to forecast (1-16)")
    ] = 7,
) -> Forecast:
    """[DATA] Look up the daily weather forecast for a city.

    Read-only: does not change anything. Returns one entry per day with conditions,
    min/max temperature (C), precipitation probability (%) and amount (mm), and max wind (km/h).
    Fails with a clear error if the city is unknown or the weather service is unavailable.
    """
    log.info("get_weather_forecast(city=%r, days=%d)", city, days)
    return await _fetch_forecast(city, days)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def save_trip_plan(
    city: Annotated[str, Field(description="Destination city")],
    date: Annotated[str, Field(description="Trip date in ISO format YYYY-MM-DD (today or later)")],
    summary: Annotated[str, Field(description="One-line summary, e.g. expected weather")],
    notes: Annotated[str, Field(description="Optional free-text notes")] = "",
) -> SavedTrip:
    """[ACTION] Save a trip to the user's saved-trips list.

    Side effect: appends the trip to the local trips file. Rejects dates that are malformed
    or in the past, and refuses to save a duplicate trip for the same city and date.
    Saved trips can be read via the resource trips://saved.
    """
    log.info("save_trip_plan(city=%r, date=%r)", city, date)
    return _store_trip(city, date, summary, notes)


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def plan_trip(
    city: Annotated[str, Field(description="City the user wants to visit")],
    days: Annotated[
        int, Field(ge=1, le=MAX_FORECAST_DAYS, description="How many upcoming days to consider (1-16)")
    ] = 7,
    preferred_temp_c: Annotated[
        float, Field(ge=-20, le=45, description="Ideal average temperature in Celsius")
    ] = 22.0,
    notes: Annotated[str, Field(description="Optional notes to store with the trip")] = "",
) -> TripPlan:
    """[ORCHESTRATION] Pick the best upcoming day to visit a city and save it as a trip.

    Coordinates the other two tools: (1) fetches the forecast (same logic as
    get_weather_forecast), (2) scores each day on rain, temperature vs. preference and wind,
    (3) saves the best day (same logic as save_trip_plan). Returns the chosen day, the reason,
    the full ranking, and the saved trip. Any failure in steps 1 or 3 is reported clearly.
    """
    log.info("plan_trip(city=%r, days=%d, preferred_temp_c=%.1f)", city, days, preferred_temp_c)

    log.info("  step 1/3 -> forecast lookup")
    forecast = await _fetch_forecast(city, days)

    log.info("  step 2/3 -> ranking %d days", len(forecast.days))
    scored = sorted(
        ((_score_day(d, preferred_temp_c), d) for d in forecast.days),
        key=lambda pair: pair[0],
        reverse=True,
    )
    # Skip days already saved for this city so re-planning suggests a new day.
    already = {t["date"] for t in _load_trips() if t["city"].lower() == forecast.city.lower()}
    candidates = [(s, d) for s, d in scored if d.date not in already]
    if not candidates:
        raise ToolError(
            f"Every day in the next {days} day(s) is already saved for {forecast.city}. "
            "Try a longer window."
        )
    best_score, best = candidates[0]

    reason = (
        f"{best.conditions}, {best.temp_min_c:.0f}-{best.temp_max_c:.0f}C, "
        f"{best.precipitation_probability_pct}% chance of rain, wind up to {best.wind_max_kmh:.0f} km/h "
        f"(score {best_score}/100, best of {len(forecast.days)} days)."
    )

    log.info("  step 3/3 -> saving %s on %s", forecast.city, best.date)
    saved = _store_trip(forecast.city, best.date, reason, notes)

    return TripPlan(
        best_day=best,
        reason=reason,
        ranking=[RankedDay(date=d.date, score=s, conditions=d.conditions) for s, d in scored],
        saved_trip=saved,
    )


@mcp.resource("trips://saved", mime_type="application/json")
def saved_trips() -> str:
    """All trips saved so far (read-only view of the trips file)."""
    return json.dumps(_load_trips(), indent=2)


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
