"""MCP server that connects an LLM to the YCharOS pipeline database.

Only the read-only server remains (see README.md):
  * server_a_readonly — read-only analytics (GRANT SELECT role in prod)

The write servers (server_b_tools / server_c_sql) were retired — editing is now
a download → edit → upload file round-trip through the app, not an MCP.

This runs as a *separate process* and is NOT wired into the Django deploy.
Everything here is built and tested against a local SQLite ``pipeline_db``
(the documented dev fallback).
"""
