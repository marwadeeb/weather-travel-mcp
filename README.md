# Weather & Travel MCP Toolkit

EECE 503P/798S Agentic Systems, Assignment C5. This is an MCP toolkit built with **FastMCP** and driven by an agent built with the **OpenAI Agents SDK**.

- **Demo video:** _add link here_
- **Written summary (Part 1):** [docs/SUMMARY.md](docs/SUMMARY.md)
- **Reflection:** [docs/REFLECTION.md](docs/REFLECTION.md)

| Category | Tool | What it does | Side effects | Failure cases handled |
|---|---|---|---|---|
| **Data** | `get_weather_forecast(city, days=7)` | Looks up the daily forecast (conditions, temperature, rain, wind) through Open-Meteo | None (read-only) | Unknown city, `days` outside 1–16, weather API timeout/unreachable/HTTP error |
| **Action** | `save_trip_plan(city, date, summary, notes="")` | Saves a trip to `data/trips.json` | Writes a file | Malformed date, past date, duplicate city+date |
| **Orchestration** | `plan_trip(city, days=7, preferred_temp_c=22, notes="")` | Forecast → score each day → save the best one | Writes a file (through the action logic) | Propagates data/action errors; skips days already saved; nothing is saved if the forecast fails |
| *Resource* | `trips://saved` | Read-only JSON list of saved trips | None | – |

## Architecture

```
 user ──► Host: OpenAI Agents SDK Agent (agent.py)
            │   LLM (Gemini free tier via OpenAI-compatible API) picks a tool
            ▼
          MCP Client (MCPServerStdio) ──JSON-RPC over stdio──► MCP Server (FastMCP, server.py)
                                                                 ├─ get_weather_forecast ─► Open-Meteo API
                                                                 ├─ save_trip_plan ───────► data/trips.json
                                                                 ├─ plan_trip  (calls both of the above)
                                                                 └─ resource trips://saved
```

The agent prints every hop: **cyan** lines are agent → client → server calls, **green** lines are results and **red `[ERROR]`** lines are tool errors coming back, and `[mcp-server]` lines are the server's own stderr log.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 automatically) and internet access. The weather API needs no key.

```bash
uv sync
```

```bash
cp .env.example .env
```

Then open `.env` and paste a **free** LLM key:

- **Gemini (default):** create a key at <https://aistudio.google.com/apikey> and set `LLM_PROVIDER=gemini`.
- **Groq (alternative):** create a key at <https://console.groq.com/keys> and set `LLM_PROVIDER=groq`.
- OpenRouter's free models and OpenAI also work. You can override the model with `LLM_MODEL`.

> **Free-tier limits:** the default model is `gemini-3.5-flash`. Each agent turn uses about 2 LLM calls, and Gemini's free tier has a per-minute/per-day quota. If you see `LLM request failed: 429` (quota) or `503` (overloaded), wait a minute or set `LLM_MODEL=gemini-3.5-flash-lite`. The session stays alive either way.

## Run

Interactive agent (type `trips` to read the resource, `quit` to exit):

```bash
uv run python -m weather_travel_mcp.agent
```

One-shot queries:

```bash
uv run python -m weather_travel_mcp.agent "What's the weather in Beirut for 3 days?" "Pick the best day this week to visit Paris"
```

Run the tools by hand without an LLM. This spawns the server over stdio and shows success and failure cases:

```bash
uv run python scripts/try_tools.py
```

Run the server alone, for example to connect it from Claude Desktop or the MCP Inspector:

```bash
uv run python -m weather_travel_mcp.server
```

> On Windows with Smart App Control, the `uv run weather-travel-agent` script shim may be blocked, so use the `python -m` forms above.

## Tests

```bash
uv run pytest -v
```

There are 16 tests (15 offline, plus 1 live test that is skipped unless `RUN_LIVE=1`). They run through a real FastMCP client (in-memory transport) with the weather API mocked, so they work offline. They cover tool discovery and schemas, each tool's happy path, every failure case listed above, the data tool's lack of side effects, and the orchestration tool's step ordering. To also hit the real API, set `RUN_LIVE=1`.

## Project layout

```
src/weather_travel_mcp/server.py   FastMCP server: 3 tools + 1 resource
src/weather_travel_mcp/agent.py    OpenAI Agents SDK host/client
scripts/try_tools.py               manual tool calls, including failure cases
tests/test_server.py               pytest suite
docs/SUMMARY.md                    Part 1 written summary
docs/REFLECTION.md                 Reflection
```

## Design notes

- **Single purpose:** scoring lives only in `plan_trip`, so the data tool never "gets the forecast *and* recommends". Geocoding is an internal step of the forecast lookup, not a user-facing tool.
- **Typed I/O:** arguments use `Annotated[..., Field(description, ge/le)]`, so invalid input is rejected by schema validation before any code runs. Returns are Pydantic models, which FastMCP publishes as output schemas.
- **Predictable failures:** every expected failure raises `ToolError` with an actionable message. The client receives `isError: true`, and the agent explains it to the user instead of inventing data.
- **Annotations:** `readOnlyHint`, `destructiveHint` and `openWorldHint` tell hosts which tools are safe to auto-approve.
