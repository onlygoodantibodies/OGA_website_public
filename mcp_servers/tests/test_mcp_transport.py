"""Drive Server A over the REAL MCP stdio transport (subprocess + protocol),
not just the in-process functions — the end-to-end path a client actually uses.

The server inherits the test env (conftest points MCP_READONLY_DATABASE_URL at
the isolated test sqlite db).
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _params(module):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return StdioServerParameters(
        command=sys.executable, args=["-m", module], env=env, cwd=REPO_ROOT)


async def _roundtrip(module, tool, args):
    async with stdio_client(_params(module)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = [t.name for t in listed.tools]
            result = await session.call_tool(tool, args)
            text = " ".join(getattr(p, "text", "") for p in (result.content or []))
            return names, text


def _run(module, tool, args):
    return asyncio.run(_roundtrip(module, tool, args))


def test_server_a_transport():
    names, text = _run("mcp_servers.server_a_readonly", "list_targets", {})
    assert "antibody_validation" in names
    assert "SNCA" in text
