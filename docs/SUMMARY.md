# Part 1 — Summary: MCP and the Fetch MCP Server

## Key MCP concepts (Hugging Face / Turing Post article)

The article argues that LLMs are "smart in a vacuum" but isolated from live data, and that wiring every model to every data source with custom code produced brittle, unscalable integrations. The Model Context Protocol (MCP), open-sourced by Anthropic in November 2024, replaces this N×M problem with an N+M one: each tool is wrapped once as an MCP server, and any MCP-aware application can use it. Compared with proprietary plugins or framework-level tool wrappers (e.g. LangChain), MCP standardizes tools at the protocol level, so agents can *dynamically discover* tools, resources and prompt templates at runtime instead of having them hard-coded.

The architecture has three roles. The **Host** is the user-facing AI application (e.g. Claude Desktop or our Agents SDK program); it owns the LLM and decides when context or actions are needed. Inside the host, each **Client** keeps a one-to-one session with a single server, speaking JSON-RPC over stdio or HTTP. The **Server** is a lightweight program exposing capabilities: *tools* (callable actions), *resources* (read-only data) and *prompts* (templates). The host therefore scales by adding clients, while servers stay unaware of the model behind them. The article places MCP in the *action* layer of an agent: it defines *how* tools are called, not *when* or *why*; that remains the planner's job. Limitations include operational overhead, immature authentication and security, rapid spec changes, and the fact that more tools do not guarantee better tool use, since description quality matters.

## Explored server: Fetch MCP Server

The reference Fetch server exposes a single tool, `fetch`, which retrieves a URL and converts the page to Markdown. It follows the class design principles well:

- **Well-defined interface:** typed arguments (`url`, `max_length=5000`, `start_index=0`, `raw=false`) with clear defaults and descriptions, plus annotations marking it read-only, idempotent and open-world.
- **Isolated functionality:** it only fetches; it does not search, summarize or store anything.
- **Predictable behaviour:** output is truncated at a fixed length, and the model pages through long documents with `start_index`. Failures (robots.txt refusal, HTTP errors, blocked private IPs) return an explicit `isError` result rather than empty text.

Critiques: the `raw` flag folds two output modes into one tool, a small step towards a multi-purpose tool. The `fetch` *prompt* skips the robots.txt check that the *tool* enforces, so the same operation behaves differently depending on the entry point. Pagination leaves the model to manage offsets itself. Finally, its README warns it can reach internal addresses, so safety depends on configuration flags, not the interface.
