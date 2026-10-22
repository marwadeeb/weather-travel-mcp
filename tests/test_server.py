"""Tests for the weather-travel MCP server.

Every test goes through a real MCP client (FastMCP in-memory transport), so tool schemas,
argument validation and error reporting are exercised exactly as an agent would see them.
The Open-Meteo API is mocked so the suite is offline and deterministic; set RUN_LIVE=1 to
also run one test against the real API.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from weather_travel_mcp import server

TODAY = date.today()
DAYS = [(TODAY + timedelta(days=i)).isoformat() for i in range(3)]

GEO_PARIS = {"results": [{"name": "Paris", "country": "France", "latitude": 48.85,
                          "longitude": 2.35, "timezone": "Europe/Paris"}]}
FORECAST_PARIS = {
    "timezone": "Europe/Paris",
    "daily": {
        "time": DAYS,
        # day 0: thunderstorm, day 1: perfect, day 2: cold drizzle
        "weather_code": [95, 1, 51],
        "temperature_2m_max": [24.0, 24.0, 9.0],
        "temperature_2m_min": [18.0, 19.0, 3.0],
        "precipitation_probability_max": [90, 5, 60],
        "precipitation_sum": [15.0, 0.0, 2.0],
        "wind_speed_10m_max": [40.0, 10.0, 20.0],
    },
}


@pytest.fixture(autouse=True)
def trips_file(tmp_path, monkeypatch):
    path = tmp_path / "trips.json"
    monkeypatch.setenv("TRIPS_FILE", str(path))
    return path


@pytest.fixture
def mock_api(monkeypatch):
    """Replace HTTP calls with canned Open-Meteo responses; records the URLs hit."""
    calls: list[str] = []

    async def fake_get_json(url, params):
        calls.append(url)
        if url == server.GEOCODING_URL:
            return GEO_PARIS if params["name"].lower() == "paris" else {"generationtime_ms": 0.1}
        return FORECAST_PARIS

    monkeypatch.setattr(server, "_get_json", fake_get_json)
    return calls


@pytest.fixture
async def client():
    async with Client(server.mcp) as c:
        yield c


# --------------------------------------------------------------------------- discovery
async def test_exactly_three_tools_and_one_resource(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {"get_weather_forecast", "save_trip_plan", "plan_trip"}
    for t in tools.values():
        assert t.description and t.output_schema  # documented + typed output
    assert tools["get_weather_forecast"].annotations.read_only_hint is True
    resources = await client.list_resources()
    assert [str(r.uri) for r in resources] == ["trips://saved"]


# --------------------------------------------------------------------------- data tool
async def test_forecast_returns_typed_days(client, mock_api):
    result = await client.call_tool("get_weather_forecast", {"city": "Paris", "days": 3})
    data = result.structured_content
    assert data["city"] == "Paris" and data["country"] == "France"
    assert [d["date"] for d in data["days"]] == DAYS
    assert data["days"][0]["conditions"] == "Thunderstorm"


async def test_forecast_unknown_city_is_clear_error(client, mock_api):
    with pytest.raises(ToolError, match="Unknown city 'Atlantis'"):
        await client.call_tool("get_weather_forecast", {"city": "Atlantis"})


async def test_forecast_rejects_out_of_range_days(client, mock_api):
    with pytest.raises(ToolError):
        await client.call_tool("get_weather_forecast", {"city": "Paris", "days": 30})
    assert mock_api == []  # rejected by schema validation before any network call


async def test_forecast_service_unavailable(client, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route to host")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(boom), **kw),
    )
    with pytest.raises(ToolError, match="Weather service is unreachable"):
        await client.call_tool("get_weather_forecast", {"city": "Paris"})


async def test_data_tool_has_no_side_effects(client, mock_api, trips_file):
    await client.call_tool("get_weather_forecast", {"city": "Paris"})
    assert not trips_file.exists()


# --------------------------------------------------------------------------- action tool
async def test_save_trip_persists(client, trips_file):
    result = await client.call_tool(
        "save_trip_plan", {"city": "Rome", "date": DAYS[1], "summary": "Sunny", "notes": "Colosseum"}
    )
    saved = result.structured_content
    assert saved["city"] == "Rome" and saved["date"] == DAYS[1]
    on_disk = json.loads(trips_file.read_text())
    assert on_disk[0]["id"] == saved["id"]


@pytest.mark.parametrize(
    "bad_date, message",
    [("21/10/2026", "Invalid date"), ((TODAY - timedelta(days=1)).isoformat(), "in the past")],
)
async def test_save_trip_rejects_bad_dates(client, trips_file, bad_date, message):
    with pytest.raises(ToolError, match=message):
        await client.call_tool("save_trip_plan", {"city": "Rome", "date": bad_date, "summary": "x"})
    assert not trips_file.exists()


async def test_save_trip_rejects_duplicates(client):
    args = {"city": "Rome", "date": DAYS[1], "summary": "x"}
    await client.call_tool("save_trip_plan", args)
    with pytest.raises(ToolError, match="already saved"):
        await client.call_tool("save_trip_plan", {**args, "city": "rome"})


async def test_resource_lists_saved_trips(client):
    await client.call_tool("save_trip_plan", {"city": "Rome", "date": DAYS[1], "summary": "x"})
    contents = await client.read_resource("trips://saved")
    assert [t["city"] for t in json.loads(contents[0].text)] == ["Rome"]


# --------------------------------------------------------------------------- orchestration tool
async def test_plan_trip_picks_best_day_and_saves_it(client, mock_api, trips_file):
    result = await client.call_tool("plan_trip", {"city": "Paris", "days": 3})
    plan = result.structured_content
    assert plan["best_day"]["date"] == DAYS[1]  # the clear, mild, calm day
    assert [r["date"] for r in plan["ranking"]][0] == DAYS[1]
    assert plan["saved_trip"]["date"] == DAYS[1]
    # It really did coordinate both steps: forecast lookup + save.
    assert mock_api == [server.GEOCODING_URL, server.FORECAST_URL]
    assert json.loads(trips_file.read_text())[0]["city"] == "Paris"


async def test_plan_trip_twice_picks_next_best_day(client, mock_api):
    await client.call_tool("plan_trip", {"city": "Paris", "days": 3})
    second = (await client.call_tool("plan_trip", {"city": "Paris", "days": 3})).structured_content
    assert second["best_day"]["date"] != DAYS[1]


async def test_plan_trip_propagates_data_failure_without_saving(client, mock_api, trips_file):
    with pytest.raises(ToolError, match="Unknown city"):
        await client.call_tool("plan_trip", {"city": "Atlantis"})
    assert not trips_file.exists()


async def test_plan_trip_fails_when_every_day_is_taken(client, mock_api):
    for d in DAYS:
        await client.call_tool("save_trip_plan", {"city": "Paris", "date": d, "summary": "x"})
    with pytest.raises(ToolError, match="already saved"):
        await client.call_tool("plan_trip", {"city": "Paris", "days": 3})


# --------------------------------------------------------------------------- live (opt-in)
@pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="set RUN_LIVE=1 to hit Open-Meteo")
async def test_live_forecast(client):
    result = await client.call_tool("get_weather_forecast", {"city": "Beirut", "days": 2})
    assert result.structured_content["country"] == "Lebanon"
    assert len(result.structured_content["days"]) == 2
