"""The usage reporter: that it is wired to the method FastMCP actually calls.

There is exactly one thing here that could be wrong *silently*, and it is not
the HTTP post — a failed post is printed to stderr and the numbers simply stop.
It is the wiring. ``instrument`` wraps the **tool manager's** ``call_tool``
because the low-level server captures the bound ``FastMCP.call_tool`` when its
handlers are set up, so a wrapper put on that attribute afterwards is one
nothing ever calls: the server keeps working, every tool answers, and the
counter reports zero for ever, which reads exactly like a connector nobody uses.
A test that greps the source for ``instrument`` passes on that. This one drives
a real tool call.

No network: ``usage.report`` is replaced, so nothing is queued and no thread
starts.
"""
from __future__ import annotations

import asyncio

import pytest

from mcp_servers.common import usage


@pytest.fixture
def reporting_on(monkeypatch):
    """Turn reporting on and collect what would have been sent."""
    monkeypatch.setenv(usage.ENV_URL, "https://example.invalid/internal/mcp-usage/")
    monkeypatch.setenv(usage.ENV_TOKEN, "t0ken")
    sent = []
    monkeypatch.setattr(
        usage, "report",
        lambda tool, client="", internal=False: sent.append(
            (tool, client, internal)))
    return sent


def _server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    @mcp.tool()
    def echo(word: str) -> str:
        return word

    return mcp


def test_a_tool_call_is_counted(reporting_on):
    mcp = usage.instrument(_server())
    asyncio.run(mcp.call_tool("echo", {"word": "hi"}))
    assert reporting_on == [("echo", "", False)]


def test_the_tool_still_answers(reporting_on):
    """The wrapper returns the tool's own result untouched — a counter that eats
    the answer is worse than no counter."""
    mcp = usage.instrument(_server())
    result = asyncio.run(mcp.call_tool("echo", {"word": "hi"}))
    assert "hi" in str(result)


def test_instrumenting_twice_counts_once(reporting_on):
    """``build_server`` may be called more than once in a process (both transport
    modes exist, and the tests build servers freely). A second wrap would double
    every figure on the impact page, silently."""
    mcp = usage.instrument(usage.instrument(_server()))
    asyncio.run(mcp.call_tool("echo", {"word": "hi"}))
    assert len(reporting_on) == 1


def test_reporting_off_leaves_the_server_alone(monkeypatch):
    """With no URL and no token, nothing is wrapped at all — a local run must not
    pay for, or be changed by, a counter it is not using."""
    monkeypatch.delenv(usage.ENV_URL, raising=False)
    monkeypatch.delenv(usage.ENV_TOKEN, raising=False)
    mcp = _server()
    usage.instrument(mcp)
    # `instrument` works by setting an instance attribute over the class's
    # method. Comparing the bound methods would not do: attribute access makes a
    # fresh bound-method object every time, so `is` is False either way.
    assert "call_tool" not in vars(mcp._tool_manager)


def test_a_client_that_says_nothing_is_not_a_crash():
    """`client_name` is read off the SDK's session object at call time; a client
    that sent no ``clientInfo``, or an SDK that renames the field, must cost the
    client's name and never the call."""
    assert usage.client_name(None) == ""
    assert usage.client_name(object()) == ""


def test_a_plain_http_url_switches_reporting_off(monkeypatch):
    """The token would be on the wire in clear. A typo in an env var is how that
    happens, so the misconfiguration costs the counter and never the secret."""
    monkeypatch.setenv(usage.ENV_TOKEN, "t0ken")
    monkeypatch.setenv(usage.ENV_URL, "http://example.invalid/internal/mcp-usage/")
    assert not usage.enabled()
    monkeypatch.setenv(usage.ENV_URL, "http://localhost:8000/internal/mcp-usage/")
    assert usage.enabled()


def test_the_request_carries_the_token_and_names_itself(monkeypatch):
    """What actually goes on the wire.

    The header half is what a live 403 turned on: an anonymous
    ``Python-urllib`` is what a bot filter bounces and what an origin log cannot
    attribute months later. The URL half is the whole contract — the reporter
    posts exactly where the env var points, and pointing it at the Cloudflare-
    proxied public hostname instead of the origin is what cost an hour on
    1 Sep 2026.
    """
    monkeypatch.setenv(usage.ENV_URL, "https://origin.invalid/internal/mcp-usage/")
    monkeypatch.setenv(usage.ENV_TOKEN, "t0ken")
    seen = {}

    class _Reply:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["body"] = req.data
        return _Reply()

    monkeypatch.setattr(usage.urllib.request, "urlopen", fake_urlopen)
    usage._post("search_antibodies", "claude-ai")

    assert seen["url"] == "https://origin.invalid/internal/mcp-usage/"
    assert seen["headers"]["authorization"] == "Bearer t0ken"
    assert seen["headers"]["user-agent"] == usage.USER_AGENT
    assert b"search_antibodies" in seen["body"]


# --- whose call was it -------------------------------------------------------
#
# The connector is the only layer that knows, and it deliberately sends a
# boolean rather than the subject. Both directions of being wrong matter and
# they are not symmetric: filing our own testing as a reader's inflates a figure
# somebody quotes in a grant, and filing a reader's call as ours deletes real
# reach. Everything unattributable therefore answers "not ours".


def _with_subject(monkeypatch, subject):
    """Stand in for the SDK's request-scoped access token."""
    import mcp.server.auth.middleware.auth_context as ctx

    class _Token:
        pass

    token = _Token()
    token.subject = subject
    monkeypatch.setattr(ctx, "get_access_token",
                        lambda: token if subject is not None else None)


def test_a_listed_subject_is_ours(monkeypatch):
    monkeypatch.setenv(usage.ENV_INTERNAL, "user_01ABC, user_01DEF")
    _with_subject(monkeypatch, "user_01DEF")
    assert usage.is_internal_caller() is True


def test_an_unlisted_subject_is_not_ours(monkeypatch):
    monkeypatch.setenv(usage.ENV_INTERNAL, "user_01ABC")
    _with_subject(monkeypatch, "user_01ZZZ")
    assert usage.is_internal_caller() is False


def test_no_list_means_nothing_is_ours(monkeypatch):
    """Unset is the safe default: the page over-counts reach visibly rather
    than under-counting it silently."""
    monkeypatch.delenv(usage.ENV_INTERNAL, raising=False)
    _with_subject(monkeypatch, "user_01ABC")
    assert usage.is_internal_caller() is False


def test_an_unauthenticated_call_is_not_ours(monkeypatch):
    monkeypatch.setenv(usage.ENV_INTERNAL, "user_01ABC")
    _with_subject(monkeypatch, None)
    assert usage.is_internal_caller() is False


def test_the_subject_never_leaves_the_process(monkeypatch):
    """The whole privacy argument in one assertion: what goes on the wire is a
    boolean, and the subject that decided it is not in the body anywhere."""
    monkeypatch.setenv(usage.ENV_URL, "https://origin.invalid/internal/mcp-usage/")
    monkeypatch.setenv(usage.ENV_TOKEN, "t0ken")
    seen = {}

    class _Reply:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(usage.urllib.request, "urlopen",
                        lambda req, timeout=None: (seen.update(body=req.data)
                                                   or _Reply()))
    usage._post("list_targets", "claude-ai", internal=True)

    assert b'"internal": true' in seen["body"]
    assert b"user_01" not in seen["body"]


def test_enabled_needs_both_halves(monkeypatch):
    """One half set is a misconfiguration, not a half-on counter: a URL with no
    token would post unauthenticated forever and be refused every time."""
    monkeypatch.setenv(usage.ENV_URL, "https://example.invalid/")
    monkeypatch.delenv(usage.ENV_TOKEN, raising=False)
    assert not usage.enabled()
    monkeypatch.setenv(usage.ENV_TOKEN, "t")
    assert usage.enabled()
