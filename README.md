# Weather-Prediction MCP Server + Agent

Homework: Build Your Own Weather-Prediction MCP Server + Agent (2026-08-08), based on the
[Day 3](https://github.com/EcZachly/databricks-lakebase-app-day-3) Agent Bricks + MCP pattern -
swapping Alpaca Markets paper trading for a weather-forecast MCP server backed by
[Open-Meteo](https://open-meteo.com/).

## Weather API + auth

**[Open-Meteo](https://open-meteo.com/)** - no signup, no API key, ~10,000 calls/day on the
free non-commercial tier. Chosen over the National Weather Service API (US-only) and
WeatherAPI.com (requires a signup + key) because it needs **zero credentials** and covers any
location worldwide, including built-in free geocoding (a city name resolves to lat/lon via
Open-Meteo's own Geocoding API - no hardcoded city list). The only secret this project has at
all is an *optional* Lakebase URL used for logging (see below); the weather tools themselves
run with no secrets configured.

## Architecture

```
Agent Bricks agent  --(MCP tool calls)-->  mcp_server/weather_mcp_server.py  --(REST, no key)-->  Open-Meteo
        |                                          |
        | (chat)                                   | (best-effort log of every tool call)
        v                                          v
     end user                                 Lakebase: weather_query_log
                                                     ^
                                                     | (reads log + ad hoc lookups)
                                            dashboard/app.py
```

- `mcp_server/` and `dashboard/` are **two separate Databricks Apps** - one serves MCP tool
  calls to the agent, the other serves a human-facing dashboard of what the agent has been
  asking. Each deploys independently from its own folder, so each carries its own copy of
  `weather_broker.py` / `lakebase.py` (same reason Day 3 duplicates `alpaca_broker.py` across
  `mcp_server/` and `dashboard/` - there's no shared-package install step across Databricks
  Apps).
- `mcp_server/weather_broker.py` is the broker/adapter: every HTTP call and JSON parse for
  Open-Meteo's Geocoding, Forecast, and Archive APIs lives here. `weather_mcp_server.py`'s
  `@mcp.tool` functions are thin passthroughs with no raw `requests` calls of their own.
- `mcp_server/lakebase.py` is optional infrastructure, not a dependency: every tool call gets
  best-effort logged to a `weather_query_log` table (see `_log_query` in
  `weather_mcp_server.py`) so the dashboard has something real to show, but a missing/unreachable
  Lakebase instance never breaks a weather lookup - it just skips the log write.

## Tools

| Tool | Purpose |
|---|---|
| `get_current_weather(location)` | Current temperature, conditions, humidity, wind. |
| `get_forecast(location, days=5)` | Multi-day forecast: highs/lows, precip chance, conditions. |
| `get_travel_recommendation(location, target_date=None)` | **Derived judgment call** (not a passthrough): umbrella/jacket recommendation + severe-weather caution, built from fixed thresholds against that day's forecast. See "Prediction logic" below. |
| `compare_weather(locations)` | *(stretch)* Current conditions across multiple cities side by side, one bad location doesn't fail the rest. |
| `get_historical_weather(location, start_date, end_date=None)` | *(stretch)* Historical daily weather for a past date/range. |

### Prediction logic (`get_travel_recommendation`)

Applies fixed thresholds to the target day's forecast, then explains which ones fired:

- **Umbrella recommended** if `precipitation_chance_pct > 40`
- **Jacket recommended** if the day's low temperature `< 50F`
- **Severe weather caution** if max wind `> 25 mph` or precipitation `> 0.5 in`

Rejects dates in the past (points the caller at `get_historical_weather` instead) and dates
beyond Open-Meteo's 16-day forecast horizon, with a clear error message either way.

## Error handling

Every tool catches `weather_broker.WeatherLookupError` (raised for an unresolvable location or
an Open-Meteo request failure) and returns `{"status": "error", "message": "..."}` - never a
stack trace. `compare_weather` isolates per-location failures so one bad city in the list
doesn't fail the whole comparison.

## Setup

### 1. Install dependencies and run both apps locally

```bash
cd mcp_server && pip install -r requirements.txt && python weather_mcp_server.py   # serves MCP on :8000
```

In a second terminal:

```bash
cd dashboard && pip install -r requirements.txt && python app.py                    # serves UI on :8001
```

No `.env` or secrets are required to exercise the weather tools themselves. Open
`http://localhost:8001` to see the dashboard (empty query log until Lakebase logging is wired
up - see step 2).

Sanity-check the MCP tools directly, e.g. with an
[MCP Inspector](https://docs.databricks.com/aws/en/agents/mcp-tools/connect-clients) pointed at
`http://localhost:8000`, or by calling `weather_broker` functions from a Python shell.

### 2. (Optional) wire up query logging via Lakebase

Reuse an existing Lakebase instance from a prior homework, or create one, then either:

- run `python setup_secrets.py` to store the connection URL as the `database/lakebase-url`
  Databricks secret (same scope/key both `app.yaml`s already point at), or
- for local dev only, `cp .env.example .env` and paste the URL into `LAKEBASE_URL`.

The `weather_query_log` table is created automatically on first write (see
`lakebase.ensure_weather_query_log_table()`); `mcp_server/schema_weather_log.sql` is provided
for a manual run if you'd rather create it up front.

### 3. Deploy both apps to Databricks Apps

Following the Day 2/3 Git-folder pattern (Apps UI, no CLI needed):

1. Push this repo to your own GitHub repo, then create a Git folder for it in your Databricks
   workspace.
2. **Deploy the MCP server app**: Compute > Apps > Create app > Custom, name it e.g.
   `weather-mcp`, point its source at the Git folder's `mcp_server/` subfolder (so it picks up
   `mcp_server/app.yaml`). Deploy it and copy its app URL.
3. **Deploy the dashboard app**: repeat, naming it e.g. `weather-dashboard`, pointing at
   `dashboard/`.

### 4. Register the MCP server as an external MCP

Follow [Connect agents to external MCPs and tools](https://docs.databricks.com/aws/en/agents/mcp-tools/connect-external):

1. **AI Gateway** > **MCPs** > **Add MCP** (or **Register external MCP**).
2. Paste the `weather-mcp` app's URL as the server endpoint (streamable HTTP).
3. Name it (e.g. `weather-forecast`) and save - Databricks introspects the server and lists all
   5 tools.

### 5. Build the Agent Bricks agent

1. **Agents** > **Agent Bricks** > **Create agent** (Custom LLM agent type works for a single
   tool-calling agent like this).
2. Under **Tools**, add the `weather-forecast` MCP server (all 5 tools).
3. Set the system prompt (below).
4. Evaluate against a few sample prompts, deploy, then chat with it.

## Agent system prompt

```
You are a weather assistant. You have no built-in knowledge of current or future weather -
every fact about temperature, precipitation, wind, or conditions MUST come from a tool call.
Never guess or make up weather data.

Tool selection:
- "What's the weather like right now in X" -> get_current_weather
- "What's the forecast for X over the next N days" -> get_forecast
- "Will it rain / should I bring an umbrella or jacket / any advice for a trip" ->
  get_travel_recommendation (do not compute this yourself from get_forecast output - always
  call the dedicated tool so the umbrella/jacket thresholds are applied consistently)
- "Compare weather in X vs Y (vs Z...)" -> compare_weather
- "What was the weather like on <past date>" -> get_historical_weather

Guardrails:
- Only answer for locations you can resolve. If a tool returns status="error" (bad location,
  API failure, or an out-of-range date), tell the user what went wrong and ask them to clarify
  or try a nearby date - do not retry blindly or fabricate an answer.
- If get_travel_recommendation rejects a date as being in the past, call
  get_historical_weather instead and describe what already happened rather than predicting it.
- Always state the resolved location name and date/time the data applies to, so the user knows
  exactly what the numbers describe.
- Keep answers concise: lead with the direct answer (yes/no, the recommendation, the
  temperature), then the supporting numbers.
```

## Demo: 3 questions and the agent's answers

*(Paste transcripts or screenshots here after chatting with the deployed Agent Bricks agent -
each entry should show the question, which tool(s) the agent called, and its final answer.)*

### 1. "Will it rain in Chicago tomorrow?"

> _Tool call:_ `get_travel_recommendation(location="Chicago, IL", target_date="<tomorrow>")`
>
> _Agent answer:_ TODO - paste here

### 2. "Should I bring a jacket to Austin this weekend?"

> _Tool call(s):_ TODO
>
> _Agent answer:_ TODO - paste here

### 3. "Compare the weather in Chicago, Austin, and Miami right now"

> _Tool call:_ `compare_weather(locations=["Chicago, IL", "Austin, TX", "Miami, FL"])`
>
> _Agent answer:_ TODO - paste here

## Files

- `mcp_server/weather_mcp_server.py` - FastMCP server exposing the 5 tools
- `mcp_server/weather_broker.py` - Open-Meteo adapter (geocoding, current, forecast, archive)
- `mcp_server/lakebase.py` - optional query-log connection helper
- `mcp_server/schema_weather_log.sql` - manual-run reference for the log table
- `mcp_server/app.yaml` / `mcp_server/requirements.txt` - Databricks App config for the MCP server
- `dashboard/app.py` - Flask dashboard (recent agent queries + manual lookup)
- `dashboard/templates/index.html` - Dashboard UI
- `dashboard/weather_broker.py` / `dashboard/lakebase.py` - copies of the same adapter/helper
  (each Databricks App deploys from its own folder)
- `dashboard/app.yaml` / `dashboard/requirements.txt` - Databricks App config for the dashboard
- `setup_secrets.py` - one-time script to store the (optional) Lakebase URL secret
- `.env.example` - local dev env var template

## Notes / known limitations

- Open-Meteo's forecast horizon is 16 days; `get_travel_recommendation` and `get_forecast`
  both clamp/reject beyond that rather than silently returning nothing.
- Query logging is best-effort and non-blocking by design - if Lakebase isn't configured, the
  weather tools still work; only the dashboard's log view stays empty.
- No secrets are committed anywhere in this repo; the only secret (`database/lakebase-url`) is
  optional and read via `WorkspaceClient().secrets` / a local `.env`, never hardcoded.
