"""Call each MCP tool by hand (no LLM) to see inputs, outputs and failure cases.

Spawns the real server over stdio, exactly as the agent does, then runs a fixed scenario:

    uv run python scripts/try_tools.py

Writes to a throwaway trips file so your real saved trips are untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

SERVER = Path(__file__).resolve().parents[1] / "src" / "weather_travel_mcp" / "server.py"


async def call(client: Client, tool: str, args: dict) -> None:
    print(f"\n>>> {tool}({json.dumps(args)})")
    result = await client.call_tool(tool, args, raise_on_error=False)
    text = result.content[0].text
    if result.is_error:
        print(f"ERROR (handled): {text}")
    else:
        print(json.dumps(json.loads(text), indent=2)[:1200])


async def main() -> None:
    trips = Path(tempfile.gettempdir()) / "weather_travel_try_tools.json"
    trips.unlink(missing_ok=True)
    transport = PythonStdioTransport(
        script_path=SERVER, python_cmd=sys.executable,
        env={**os.environ, "TRIPS_FILE": str(trips)},
    )
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    async with Client(transport) as client:
        print("Tools:", [t.name for t in await client.list_tools()])

        print("\n=== DATA tool ===")
        await call(client, "get_weather_forecast", {"city": "Beirut", "days": 3})
        await call(client, "get_weather_forecast", {"city": "Qwzxbnmville"})   # failure

        print("\n=== ACTION tool ===")
        await call(client, "save_trip_plan", {"city": "Byblos", "date": tomorrow, "summary": "Beach day"})
        await call(client, "save_trip_plan", {"city": "Byblos", "date": "2020-01-01", "summary": "x"})  # failure
        await call(client, "save_trip_plan", {"city": "Byblos", "date": tomorrow, "summary": "again"})  # failure

        print("\n=== ORCHESTRATION tool ===")
        await call(client, "plan_trip", {"city": "London", "days": 5, "notes": "Museums"})
        await call(client, "plan_trip", {"city": "Qwzxbnmville"})               # failure

        print("\n=== RESOURCE trips://saved ===")
        print((await client.read_resource("trips://saved"))[0].text)

    trips.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
