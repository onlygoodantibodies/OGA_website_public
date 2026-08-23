"""End-to-end smoke test of Server A over the REAL MCP stdio transport.

Unlike the pytest suite (which calls the internal op_/query functions), this
launches the server as a subprocess, speaks MCP to it, lists its tools, and
calls a couple — exercising the actual tool schemas + JSON serialization a real
LLM client would use. Runs against the LOCAL seeded ``db_pipeline.sqlite3``.

    python mcp_servers/smoke_client.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["DEBUG"] = "True"
    env.setdefault("PIPELINE_BASE_URL", "http://localhost:8000")
    return env


def _params(module):
    return StdioServerParameters(
        command=sys.executable, args=["-m", module], env=_env(), cwd=REPO_ROOT)


def _text(result):
    parts = getattr(result, "content", []) or []
    out = []
    for p in parts:
        out.append(getattr(p, "text", str(p)))
    s = " ".join(out)
    return (s[:180] + "…") if len(s) > 180 else s


async def _drive(label, module, calls):
    print(f"\n=== {label}  ({module}) ===")
    async with stdio_client(_params(module)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"  tools ({len(names)}): {', '.join(names)}")
            for tool, args in calls:
                res = await session.call_tool(tool, args)
                print(f"  call {tool}({args}) -> {_text(res)}")


async def main():
    await _drive("Server A — read-only analytics", "mcp_servers.server_a_readonly", [
        ("list_targets", {}),
        ("antibody_validation", {"catalogue": "ab212184"}),
        ("search_antibodies", {"text": "ab212184"}),
    ])
    print("\nServer A responded over MCP stdio. ✓")


if __name__ == "__main__":
    asyncio.run(main())
