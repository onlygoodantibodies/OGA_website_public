"""The HTTP + bearer-token transport (Path 2): the servers spoken to over HTTP,
the way a hosted Render service exposes them to Claude. Spawns the real ASGI app
under uvicorn and drives it over the network.

Covers: a tier is dark until its token is set; wrong/absent token -> 401; the
right token -> full MCP round-trip; and per-tier token isolation (a tier's token
can't open another tier).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _serve(tokens: dict):
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PORT"] = str(port)
    env.update(tokens)
    # Ensure only the intended tier is enabled (drop any inherited token).
    for var in ("MCP_READONLY_TOKEN",):
        if var not in tokens:
            env.pop(var, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_servers.http_server"],
        env=env, cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("http_server did not become ready")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


async def _roundtrip(url, token, tool, args):
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            listed = await s.list_tools()
            res = await s.call_tool(tool, args)
            text = " ".join(getattr(p, "text", "") for p in (res.content or []))
            return [t.name for t in listed.tools], text


def test_health_lists_enabled_tiers():
    with _serve({"MCP_READONLY_TOKEN": "ro"}) as base:
        data = httpx.get(f"{base}/healthz").json()
        assert data["enabled"] == ["readonly"]
        assert data["endpoints"]["readonly"] == "/readonly/mcp"


def test_missing_and_wrong_token_rejected():
    with _serve({"MCP_READONLY_TOKEN": "ro"}) as base:
        assert httpx.post(f"{base}/readonly/mcp").status_code == 401
        assert httpx.post(f"{base}/readonly/mcp",
                          headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_retired_write_tiers_are_dark():
    with _serve({"MCP_READONLY_TOKEN": "ro"}) as base:
        # the retired tools/admin write tiers are not mounted at all
        assert httpx.post(f"{base}/tools/mcp").status_code == 404
        assert httpx.post(f"{base}/admin/mcp").status_code == 404


def test_valid_token_full_roundtrip():
    with _serve({"MCP_READONLY_TOKEN": "ro"}) as base:
        names, text = asyncio.run(_roundtrip(
            f"{base}/readonly/mcp", "ro", "antibody_validation", {"catalogue": "ab212184"}))
        assert "antibody_validation" in names
        assert "SNCA" in text


def test_readonly_connector_is_database_only():
    # The tutor tools were removed while the tutor is reworked: the public
    # connector now exposes the antibody DATA tools only, no education tools.
    with _serve({"MCP_READONLY_TOKEN": "ro"}) as base:
        names, text = asyncio.run(_roundtrip(
            f"{base}/readonly/mcp", "ro", "antibody_validation", {"catalogue": "ab212184"}))
        # data tools present
        assert {"antibody_validation", "target_report", "antibodies_by_support",
                "list_targets", "search_antibodies"} <= set(names)
        # education / tutor tools are gone
        assert not ({"learning_overview", "choosing_walkthrough", "get_module",
                     "get_module_quiz", "submit_module_quiz", "get_plan_template",
                     "grade_plan", "classify_control", "certificate_claim_link",
                     "target_evidence"} & set(names))
        assert "SNCA" in text
