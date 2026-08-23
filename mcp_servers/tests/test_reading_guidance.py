"""The paper-reading guidance, and the one property that makes it worth having.

MCP has three primitives — tools, prompts, resources — and no skills primitive.
That was checked against the SDK this server pins (1.28.1) and against the newest
published (2.0.0, whose request set adds tasks, discovery and subscriptions and
still has no skills), rather than assumed. So a server cannot serve a skill as a
first-class thing; it can serve the same document through the doors that exist.

Two doors, because they are opened by different people: a MODEL calls the tool
when it decides it needs help; a READER picks the prompt when they decide they
want it.

**One file behind both, and behind the skill.** A copy of guidance is guidance
that drifts, and drift here is invisible until two readers of one paper disagree
about what they were told. That is what the last test pins, and it is the only
one of these that can fail for an interesting reason.
"""
from __future__ import annotations

import asyncio
import io
import json
import os

import pytest

from mcp_servers.common import content_pack, portal


@pytest.fixture(scope="module", autouse=True)
def _django(_seeded_pipeline_db):
    portal.setup()


@pytest.fixture(scope="module")
def server():
    from mcp_servers import server_a_readonly
    return server_a_readonly.build_server()


def _tool(server, name, args=None):
    out = asyncio.run(server.call_tool(name, args or {}))
    return json.loads(out[0].text)


def test_both_doors_are_registered(server):
    tools = {t.name for t in asyncio.run(server.list_tools())}
    prompts = {p.name for p in asyncio.run(server.list_prompts())}
    assert "how_to_read_a_paper" in tools
    assert "how_to_read_a_paper" in prompts
    # The rules that bind are fetchable in full too — a shorter scan_controls
    # reply is a shorter reply, not a smaller entitlement.
    assert "controls_rubric" in tools


def test_the_tool_serves_the_guidance(server):
    data = _tool(server, "how_to_read_a_paper")
    assert data["available"] is True
    assert data["text"].lstrip().startswith("# ")
    # Frontmatter is for the skill loader; a model asking wants the document.
    assert not data["text"].lstrip().startswith("---")


def test_the_prompt_serves_the_same_words(server):
    got = asyncio.run(server.get_prompt("how_to_read_a_paper", {}))
    assert got.messages[0].content.text == _tool(server, "how_to_read_a_paper")["text"]


def test_a_deployment_without_the_file_says_so_rather_than_serving_nothing(
        server, monkeypatch):
    """`None` is a real answer. An empty string would read as guidance that has
    nothing to say, which is the failure this repo keeps meeting: a silent
    omission that looks like success."""
    monkeypatch.setattr(content_pack, "reading_guidance", lambda: None)
    data = _tool(server, "how_to_read_a_paper")
    assert data["available"] is False
    assert "not present in this deployment" in data["note"]
    # …and says what is unaffected, so it does not read as the whole surface
    # being broken.
    assert "scan_controls" in data["note"]


def test_the_served_text_is_the_skill_file_itself():
    """The point of the exercise. If someone pastes the guidance into the server
    instead of reading the file, this is what notices."""
    raw = io.open(content_pack.SKILL_PATH, encoding="utf-8").read()
    body = raw.split("\n---", 1)[1].lstrip("-\n") if raw.startswith("---") else raw
    assert content_pack.reading_guidance() == body.strip()
    assert os.path.basename(content_pack.SKILL_PATH) == "SKILL.md"
