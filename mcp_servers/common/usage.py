"""Report each tool call to the website, so usage outlives the log.

This service's own log was the only record that anybody had ever used it, and
Render keeps seven days of it. On 31 Aug 2026 that made "is anybody using the
MCP server?" answerable for one week and unanswerable for every week before —
and the WorkOS user list that looks like the answer is a list of *accounts*,
which counts somebody who signed in once and never called a tool the same as a
partner using it daily.

So each call is reported to ``/internal/mcp-usage/`` on the website, which keeps
one row per (day, tool, client) and draws it on the pipeline's impact page.
``core/mcp_usage.py`` is the other half.

Three rules, all of them the same rule.

**It never blocks a tool call, and never breaks one.** The report goes on a
bounded queue and a daemon thread posts it; the tool handler does not wait and
never sees an error. A reporting failure costs a number, which is worth strictly
less than an answer.

**It drops rather than grows.** A full queue discards the report — the volume
being measured is a few dozen calls a week, so a backlog means the website is
unreachable, and holding reports in memory for a service that reachability
problem cannot end well.

**It says the client, never the caller.** The MCP client's own name
(``claude-ai``, ``claude-code``, ``chatgpt``) is software, and the OAuth subject
this server does know is deliberately not sent.

**It says whether the caller was one of us, as a boolean.** ``MCP_INTERNAL_SUBJECTS``
holds our own OAuth subjects (WorkOS user ids), comma-separated; the report
carries ``internal: true`` when the caller matches and never the subject itself.
That is the whole of what identity is used for here — the impact page can keep
our testing out of a figure somebody quotes in a grant, and the table still
holds no identities. **A call this cannot attribute is external**: no token, an
unreadable one, or a subject nobody listed. Over-reporting our own exercising of
the connector is the honest direction; filing a stranger's call as ours would
delete real reach.

**Point ``MCP_USAGE_URL`` at Render's private network, not a public hostname.**
Live it is ``http://oga-website:10000/internal/mcp-usage/`` — the website's
internal name (its Render slug) and port. Neither public name will do:

- The apex, ``onlygoodantibodies.co.uk``, is proxied through Cloudflare with Bot
  Fight Mode on, and this posts from ``urllib`` — so a report there is refused
  **403 at the edge**, never reaches Django, and appears in no origin log at all.
  Measured 1 Sep 2026: every call logged ``report failed: HTTP Error 403`` while
  the site's access log had no such request in it.
- ``oga-website.onrender.com`` was the answer from 1 Sep until **27 Sep 2026**,
  when the owner switched Render's public subdomain off so nothing could reach
  the origin without passing Cloudflare. From that moment every report there
  failed, and failed quietly, because a report is telemetry and is dropped.

The private address never leaves Render: both services are on a paid plan in
one region and workspace, which is what Render's private network needs. It
is plain ``http`` because the private network carries no TLS, and that is the
one ``http`` URL ``enabled`` accepts besides localhost — a **single-label**
host, which only a private network's DNS can resolve, so a typo naming a public
site over ``http`` still switches reporting off rather than sending the token in
clear. The website must list the name in ``ALLOWED_HOSTS``, or Django refuses
the report 400 before the view is reached.

Off unless both ``MCP_USAGE_URL`` and ``MCP_USAGE_TOKEN`` are set — and off
rather than insecure when that URL is not https, since a typo in an env var
would otherwise put the shared token on the wire in clear on every call. A local
run needs no configuration to stay quiet.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

ENV_URL = "MCP_USAGE_URL"
ENV_TOKEN = "MCP_USAGE_TOKEN"

#: Comma-separated OAuth subjects (WorkOS user ids, ``user_01...``) that are
#: OGA's own. Unset means nothing is ever marked internal, which is the safe
#: default: the page then over-counts reach visibly rather than under-counting
#: it silently.
ENV_INTERNAL = "MCP_INTERNAL_SUBJECTS"

#: Deep enough to ride out a slow reply, shallow enough that an unreachable
#: website costs a fixed amount of memory and then starts dropping.
QUEUE_MAX = 500

#: A report is telemetry: it gets one short attempt and no retry.
TIMEOUT_SECONDS = 5

#: Who is calling, in the origin's access log. ``urllib``'s default
#: ``Python-urllib/3.x`` is both anonymous — a line nobody can attribute months
#: later — and the exact signature a bot filter bounces, which is how these
#: reports spent their first hour being refused 403 by Cloudflare. Naming the
#: service is honest and is not an attempt to look like a browser: the fix for
#: the edge is posting to the origin, not disguising the caller.
USER_AGENT = "OGA-MCP-usage/1.0 (+https://oga-mcp.onrender.com)"

_queue: "queue.Queue | None" = None
_worker: "threading.Thread | None" = None
_lock = threading.Lock()


def enabled() -> bool:
    """Both halves set, and the URL one the token can safely be sent to.

    A URL over plain http to a public host would put the shared token on the
    wire in clear on every call, and a typo in an env var is exactly how that
    happens — so such a URL switches reporting **off** rather than reporting
    insecurely. Two ``http`` hosts are exempt: localhost, which is the local
    test of this file, and a single-label name such as ``oga-website``, which
    resolves only on Render's private network and so never crosses the internet.
    """
    url = (os.environ.get(ENV_URL) or "").strip()
    if not url or not (os.environ.get(ENV_TOKEN) or "").strip():
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme != "http" or not parts.hostname:
        return False
    return (parts.hostname in ("127.0.0.1", "localhost")
            or _is_private_network_name(parts.hostname))


def _is_private_network_name(host: str) -> bool:
    """A bare service name — no dot, and not an IP address.

    Render's private network names a service by its slug (``oga-website``), and
    a name with no dot cannot be resolved by public DNS, so a URL that uses one
    cannot send the token anywhere but this network. A dotted name is refused
    even if it would resolve privately, because the rule has to be checkable
    from the string alone.
    """
    return "." not in host and ":" not in host


def internal_subjects() -> set:
    """Our own OAuth subjects, from the environment. Read per call, not cached:
    the set is tiny and an env change should not need a redeploy to take."""
    raw = os.environ.get(ENV_INTERNAL) or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_internal_caller() -> bool:
    """Whether the call being served is one of ours.

    Reads the authenticated subject out of the request context and compares it;
    the subject itself never leaves this function. Every failure — no auth at
    all, an SDK that moved the accessor, a caller nobody listed — answers
    ``False``, because a call we cannot attribute is somebody else's until
    proven otherwise.
    """
    ours = internal_subjects()
    if not ours:
        return False
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        subject = getattr(token, "subject", None) if token else None
        return bool(subject) and subject in ours
    except Exception:  # noqa: BLE001 — attribution never breaks a call
        return False


def _post(tool: str, client: str, internal: bool = False) -> None:
    url = (os.environ.get(ENV_URL) or "").strip()
    token = (os.environ.get(ENV_TOKEN) or "").strip()
    body = json.dumps({"tool": tool, "client": client,
                       "internal": bool(internal)}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT,
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        resp.read()


def _run(q: "queue.Queue") -> None:
    while True:
        tool, client, internal = q.get()
        try:
            _post(tool, client, internal)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Stderr rather than silence, for the same reason `audit.py` does it:
            # a counter that has quietly stopped counting reads exactly like a
            # service nobody is using, which is the question it exists to answer.
            print(f"[mcp usage] report failed: {exc}", file=sys.stderr)
        finally:
            q.task_done()


def _ensure_worker() -> "queue.Queue | None":
    global _queue, _worker
    if not enabled():
        return None
    with _lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=QUEUE_MAX)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, args=(_queue,),
                                       name="mcp-usage", daemon=True)
            _worker.start()
    return _queue


def report(tool: str, client: str = "", internal: bool = False) -> None:
    """Queue one tool call. Never raises, never waits.

    ``internal`` is resolved by the caller, on the request's own thread: the
    worker thread has no request context to read it from.
    """
    try:
        q = _ensure_worker()
        if q is None or not tool:
            return
        q.put_nowait((str(tool)[:64], str(client or "")[:64], bool(internal)))
    except queue.Full:
        pass
    except Exception as exc:  # noqa: BLE001 — telemetry must not reach a caller
        print(f"[mcp usage] could not queue a report: {exc}", file=sys.stderr)


def client_name(context) -> str:
    """The connecting client's own name, or ``""`` when it did not say.

    **Two places a client names itself, and the hosted server can only read
    one of them.** ``clientInfo`` arrives once, in the ``initialize`` request;
    the hosted server runs ``stateless_http`` (see ``server_a_readonly``), so
    every request gets a fresh session and the one that carries a tool call
    has never seen an ``initialize`` — ``client_params`` is ``None`` and the
    name is gone. From the day that went live, 573 of 609 external calls on
    the impact page read *did not say*, including one researcher's 535-call
    day through Claude. So a missing ``clientInfo`` falls back to the
    ``User-Agent`` of the request carrying the call, which every HTTP client
    sends on every request (``user_agent_name``).

    Every access here is defensive: the shape is the SDK's, a client may send
    neither, and the whole point of this module is that it cannot break the
    call it is measuring.
    """
    try:
        params = context.session.client_params
        name = (params.clientInfo.name or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001
        pass
    try:
        return user_agent_name(
            context.request_context.request.headers.get("user-agent"))
    except Exception:  # noqa: BLE001
        return ""


def user_agent_name(header) -> str:
    """The product name a ``User-Agent`` opens with — ``openai-mcp/1.0.0`` is
    ``openai-mcp``. The version is dropped so one client is one row on the
    impact page, not a row per release.

    ``Mozilla/…`` answers ``""``: that token is every browser's and most
    libraries' imitation of one, so it names nothing, and "did not say" is
    the honest reading of it rather than a client called Mozilla.
    """
    token = (header or "").strip().split(" ", 1)[0]
    name = token.split("/", 1)[0].strip()
    if not name or name.lower() == "mozilla":
        return ""
    return name[:64]


def instrument(mcp):
    """Count every tool call this server serves, present and future.

    Wraps the **tool manager's** ``call_tool`` and not ``FastMCP.call_tool``:
    the low-level server captures the bound ``FastMCP.call_tool`` when the
    handlers are set up, so replacing that attribute afterwards is a wrapper
    nothing calls — the shape of every "the test greps for the string and the
    wiring is wrong" defect in this repo. The manager's method is looked up at
    call time.

    Wrapping one method rather than decorating each tool is the point: a tool
    added later is counted without anybody remembering to.
    """
    if not enabled():
        return mcp
    manager = getattr(mcp, "_tool_manager", None)
    original = getattr(manager, "call_tool", None)
    if original is None or getattr(original, "_oga_usage_wrapped", False):
        return mcp

    async def call_tool(name, arguments, context=None, **kwargs):
        # Counted before the call, so a tool that raises is still counted as
        # somebody having asked. "Served" is a claim this cannot make; "asked
        # for" is one it can.
        report(name, client_name(context), is_internal_caller())
        return await original(name, arguments, context=context, **kwargs)

    call_tool._oga_usage_wrapped = True
    manager.call_tool = call_tool
    return mcp
