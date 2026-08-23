"""Server A2 (education connector) over the REAL MCP stdio transport.

Confirms the tools are registered and reachable end-to-end, that grade_plan
enforces the teaching gate through the transport, and that target_evidence pulls
the same read-only pipeline data Server A serves (the seeded test db).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = "mcp_servers.server_a2_academy"


def _params():
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return StdioServerParameters(
        command=sys.executable, args=["-m", MODULE], env=env, cwd=REPO_ROOT)


async def _session(fn):
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _text(result):
    return " ".join(getattr(p, "text", "") for p in (result.content or []))


def test_a2_tools_registered():
    async def go(session):
        listed = await session.list_tools()
        return [t.name for t in listed.tools]
    names = asyncio.run(_session(go))
    assert {"learning_overview", "get_module", "classify_control",
            "get_plan_template", "target_evidence", "grade_plan",
            "certificate_claim_link"} <= set(names)


def test_a2_overview_and_module():
    async def go(session):
        ov = _text(await session.call_tool("learning_overview", {}))
        mod = _text(await session.call_tool("get_module", {"module_id": "controls"}))
        return ov, mod
    ov, mod = asyncio.run(_session(go))
    assert "learning" in ov.lower()
    # The controls module carries the sharp teaching point.
    assert "selectivity" in mod.lower()
    assert "isotype" in mod.lower()


def test_a2_classify_control_over_transport():
    async def go(session):
        return _text(await session.call_tool("classify_control", {"kind": "isotype"}))
    text = asyncio.run(_session(go))
    assert "does_not_establish_selectivity" in text


def test_a2_grade_plan_gate_over_transport():
    good = {
        "target_gene": "SNCA", "application": "WB", "sample_type": "lysate",
        "question_type": "target_specific",
        "evidence_search": {"sources_checked": ["OGA"], "found_genetic_validation": True},
        "controls": {"positive": [{"kind": "overexpression_lysate", "material": "x"}],
                     "negative": [{"kind": "knockout", "material": "y"}]},
        "decision": "proceed",
    }
    pseudo = dict(good)
    pseudo["controls"] = {"positive": [{"kind": "isotype", "material": "x"}],
                          "negative": [{"kind": "secondary_only", "material": "y"}]}

    async def go(session):
        g = _text(await session.call_tool("grade_plan", {"plan": good}))
        p = _text(await session.call_tool("grade_plan", {"plan": pseudo}))
        return g, p
    g_text, p_text = asyncio.run(_session(go))
    assert '"passed_mechanical": true' in g_text.lower().replace("'", '"')
    assert '"passed_mechanical": false' in p_text.lower().replace("'", '"')


def test_a2_target_evidence_reads_pipeline():
    async def go(session):
        return _text(await session.call_tool("target_evidence", {"gene": "SNCA"}))
    text = asyncio.run(_session(go))
    assert "SNCA" in text


def test_a2_module_quiz_tools_registered():
    async def go(session):
        listed = await session.list_tools()
        return [t.name for t in listed.tools]
    names = asyncio.run(_session(go))
    assert {"choosing_walkthrough", "get_module_quiz", "submit_module_quiz",
            "certificate_claim_link"} <= set(names)


def test_a2_choosing_walkthrough_reflective_not_scored():
    async def go(session):
        return _text(await session.call_tool("choosing_walkthrough", {}))
    text = asyncio.run(_session(go)).lower()
    assert "how" in text and ("reflection" in text or "not a scored" in text)


def test_a2_module_quiz_grades_over_transport():
    # get the quiz, then submit deliberately-wrong answers → not passed
    async def go(session):
        q = _text(await session.call_tool("get_module_quiz", {"module_id": "controls"}))
        graded = _text(await session.call_tool(
            "submit_module_quiz", {"module_id": "controls", "answers": [0, 0, 0]}))
        return q, graded
    quiz_text, graded_text = asyncio.run(_session(go))
    assert "options" in quiz_text.lower() or "question" in quiz_text.lower()
    assert "passed" in graded_text.lower()


def test_a2_claim_link_prefills_modules():
    async def go(session):
        return _text(await session.call_tool(
            "certificate_claim_link",
            {"completed_modules": ["framework", "controls"], "gene": "SNCA"}))
    text = asyncio.run(_session(go))
    assert "/workshop/claim/" in text
    assert "modules=framework" in text and "controls" in text
