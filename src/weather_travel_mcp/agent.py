"""OpenAI Agents SDK host that connects to the weather-travel MCP server over stdio.

The agent (Host) owns an MCP Client session to our FastMCP server. Each turn it lists the
server's tools, lets the LLM pick one, and forwards the call through the client to the server.
Tool calls and results are printed so the full round trip is visible during the demo.

Usage:
    uv run python -m weather_travel_mcp.agent                          # interactive chat
    uv run python -m weather_travel_mcp.agent "Plan a trip to Rome"    # one-shot query

The LLM is any OpenAI-compatible endpoint configured in .env (see .env.example). The default
is Google Gemini's free tier.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date

from dotenv import load_dotenv
from openai import APIError, AsyncOpenAI

from agents import Agent, ItemHelpers, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.mcp import MCPServerStdio

# Free/cheap OpenAI-compatible providers. Override any field with LLM_BASE_URL / LLM_MODEL.
PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}

CYAN, GREEN, YELLOW, RED, DIM, RESET = (
    "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m",
)


def build_model() -> OpenAIChatCompletionsModel:
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if provider not in PROVIDERS:
        sys.exit(f"Unknown LLM_PROVIDER '{provider}'. Choose one of: {', '.join(PROVIDERS)}")
    default_url, default_model = PROVIDERS[provider]
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        sys.exit("LLM_API_KEY is not set. Copy .env.example to .env and add your key.")

    # Tracing uploads to OpenAI's dashboard and needs an OpenAI key; skip it otherwise.
    set_tracing_disabled(provider != "openai")

    # Free tiers are often overloaded (503) or slow: retry a few times, but never hang.
    client = AsyncOpenAI(
        base_url=os.environ.get("LLM_BASE_URL", default_url),
        api_key=api_key,
        timeout=45,
        max_retries=3,
    )
    model = os.environ.get("LLM_MODEL", default_model)
    print(f"{DIM}LLM: {provider} / {model}{RESET}")
    return OpenAIChatCompletionsModel(model=model, openai_client=client)


def build_server() -> MCPServerStdio:
    # Launch the server with the same interpreter so it shares this virtualenv.
    return MCPServerStdio(
        name="weather-travel",
        params={"command": sys.executable, "args": ["-m", "weather_travel_mcp.server"]},
        cache_tools_list=True,
        client_session_timeout_seconds=30,  # forecast calls hit an external API
    )


INSTRUCTIONS = f"""You are a friendly weather and travel planning assistant. Today is {date.today().isoformat()}.

Choose the single most appropriate tool:
- get_weather_forecast: the user only wants to know the weather.
- save_trip_plan: the user has already chosen a specific city AND date and wants it saved.
- plan_trip: the user wants you to choose the best day to visit a city (and save it).

If a tool returns an error, explain it to the user plainly and suggest a fix; do not invent data.
Keep answers short and mention the key numbers (temperature, rain chance)."""


def _is_json_object(text: object) -> bool:
    """Our tools return a JSON object on success and a plain error message on failure."""
    try:
        return isinstance(json.loads(str(text)), dict)
    except ValueError:
        return False


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " ..."


async def ask(agent: Agent, query: str, history: list | None = None) -> list:
    """Run one user turn, printing each hop of the agent <-> MCP round trip."""
    inputs = (history or []) + [{"role": "user", "content": query}]
    result = Runner.run_streamed(agent, inputs, max_turns=8)
    try:
        async for event in result.stream_events():
            if event.type != "run_item_stream_event":
                continue
            item = event.item
            if isinstance(item, ToolCallItem):
                raw = item.raw_item
                name = getattr(raw, "name", None) or raw.get("name")
                args = getattr(raw, "arguments", None) or raw.get("arguments")
                print(f"{CYAN}  agent -> MCP client -> server : {name}({args}){RESET}")
            elif isinstance(item, ToolCallOutputItem):
                output = item.output
                if isinstance(output, dict):  # MCP content block -> its text
                    output = output.get("text", output)
                colour = GREEN if _is_json_object(output) else RED  # errors are plain text
                label = "result" if colour == GREEN else "ERROR"
                print(f"{colour}  server -> MCP client -> agent : [{label}] {_short(output)}{RESET}")
            elif item.type == "message_output_item":
                print(f"\n{YELLOW}assistant>{RESET} {ItemHelpers.text_message_output(item)}\n")
    except APIError as exc:
        # LLM-side failure (overloaded, quota, bad key): report it and keep the session alive.
        print(f"{RED}  LLM request failed: {_short(exc, 200)}{RESET}\n")
        return history or []
    return result.to_input_list()


async def run(queries: list[str]) -> None:
    load_dotenv()
    model = build_model()
    async with build_server() as server:
        tools = await server.list_tools()
        print(f"{DIM}Connected to MCP server 'weather-travel' with tools: "
              f"{', '.join(t.name for t in tools)}{RESET}\n")
        agent = Agent(
            name="Travel Assistant",
            instructions=INSTRUCTIONS,
            model=model,
            mcp_servers=[server],
        )

        if queries:
            for q in queries:
                print(f"{YELLOW}user>{RESET} {q}")
                await ask(agent, q)
            return

        print("Ask about the weather or plan a trip. Type 'trips' to read saved trips, 'quit' to exit.\n")
        history: list = []
        while True:
            try:
                q = input(f"{YELLOW}user>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in {"quit", "exit", "q"}:
                break
            if q.lower() == "trips":
                # Read the MCP resource directly through the client session.
                res = await server.read_resource("trips://saved")
                trips = json.loads(res.contents[0].text)
                print(json.dumps(trips, indent=2) if trips else "(no saved trips)")
                continue
            if q:
                history = await ask(agent, q, history)


def main() -> None:
    if sys.platform == "win32":
        os.system("")  # enable ANSI colours in the Windows console
    asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
