"""
Weather dashboard: a small Flask app to WATCH what the Agent Bricks agent
is asking the weather MCP server (weather_mcp_server.py) via its recent
query log, plus an ad hoc lookup form for manually checking a location.

This app never calls the MCP tools itself - it reads weather_query_log
rows logged by the MCP server (see _log_query in weather_mcp_server.py)
via Lakebase, and uses its own copy of weather_broker.py only for the
manual "look up a location" form below the log.

Deploy this as its OWN Databricks App (separate from weather_mcp_server.py) -
one app serves MCP tool calls, the other serves the human-facing UI.

Run locally:
    python app.py
"""

import os

from flask import Flask, jsonify, render_template, request

import lakebase
import weather_broker
from weather_broker import WeatherLookupError

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page)."""
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Dashboard UI: recent agent queries/predictions + a manual lookup form."""
    return render_template("index.html")


@app.route("/api/recent")
def api_recent():
    """Recent MCP tool calls logged by the agent, most recent first."""
    limit = int(request.args.get("limit", 25))
    try:
        lakebase.ensure_weather_query_log_table()
        rows = lakebase.run_query(
            """
            SELECT user_email, tool_name, location, query_date, result_summary, status, created_at
            FROM weather_query_log
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return jsonify({"rows": rows})
    except Exception as exc:
        # Lakebase not configured yet (e.g. fresh local dev) - degrade to an
        # empty log instead of a 500, so the dashboard still loads.
        return jsonify({"rows": [], "warning": f"Query log unavailable: {exc}"})


@app.route("/api/lookup")
def api_lookup():
    """Ad hoc current-conditions lookup, for manually checking a location in the UI."""
    location = request.args.get("location", "")
    if not location:
        return jsonify({"error": "location query param is required"}), 400
    try:
        return jsonify(weather_broker.get_current_conditions(location))
    except WeatherLookupError as exc:
        return jsonify({"error": str(exc)}), 400


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("FLASK_RUN_PORT", 8001)))
    app.run(debug=True, host=host, port=port)
