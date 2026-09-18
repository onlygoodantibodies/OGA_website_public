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

**Point ``MCP_USAGE_URL`` at the ORIGIN, not the public hostname.** Live it is
``https://oga-website.onrender.com/internal/mcp-usage/``. The public apex is
proxied through Cloudflare with Bot Fight Mode on, and this posts from
``urllib`` — so a report to ``onlygoodantibodies.co.uk`` is refused **403 at the
edge**, never reaches Django, and appears in no origin log at all. Measured
1 Sep 2026: every call logged ``report failed: HTTP Error 403`` while the site's
access log had no such request in it. The origin address also keeps the shared
token off the public edge and off a Cloudflare dashboard nothing in git can see.

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

    A URL over plain http would put the shared token on the wire in clear on
    every call, and a typo in an env var is exactly how that happens — so a
    non-https URL switches reporting **off** rather than reporting insecurely.
    Localhost is exempt: that is the local test of this file.
    """
    url = (os.environ.get(ENV_URL) or "").strip()
    if not url or not (os.environ.get(ENV_TOKEN) or "").strip():
        return False
    return (url.startswith("https://")
            or url.startswith("http://127.0.0.1")
            or url.startswith("http://localhost"))


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

    Every access here is defensive: the shape is the SDK's, a client may send
    no ``clientInfo`` at all, and the whole point of this module is that it
    cannot break the call it is measuring.
    """
    try:
        params = context.session.client_params
        return (params.clientInfo.name or "").strip()
    except Exception:  # noqa: BLE001
        return ""


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
