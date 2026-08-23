"""Filesystem anchors shared by the servers.

``REPO_ROOT`` is the Django project root (the directory that holds
``manage.py`` and ``OGA_website/settings.py``) — the parent of ``mcp_servers/``.
"""
from __future__ import annotations

import os

# mcp_servers/common/paths.py -> mcp_servers/common -> mcp_servers -> repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The documented local dev fallback for pipeline_db (see CLAUDE.md "Run locally").
LOCAL_PIPELINE_SQLITE = os.path.join(REPO_ROOT, "db_pipeline.sqlite3")

MCP_DIR = os.path.join(REPO_ROOT, "mcp_servers")
