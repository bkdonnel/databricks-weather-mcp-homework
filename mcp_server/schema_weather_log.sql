-- weather_query_log table: records every MCP tool call the agent makes so
-- the dashboard app can show recent agent queries/predictions.
-- Run this SQL against your Lakebase Postgres database, or just let
-- lakebase.ensure_weather_query_log_table() create it automatically on
-- first use (both do the same thing).

CREATE TABLE IF NOT EXISTS weather_query_log (
    id SERIAL PRIMARY KEY,
    user_email VARCHAR(255),
    tool_name VARCHAR(50) NOT NULL,
    location VARCHAR(255),
    query_date DATE,
    result_summary TEXT,
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_weather_query_log_created_at ON weather_query_log(created_at DESC);
