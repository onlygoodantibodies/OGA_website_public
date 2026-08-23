"""HTTP transport — expose the MCP server over HTTPS on one Render Web Service.

Only the read-only tier remains (Servers B/C, the write tiers, were retired —
editing is now a download → edit → upload file round-trip through the app; see
the ``VISION.md`` banner). The tier framing is kept so a second read-only tier could be
added later, but today there is exactly one:

    /readonly/mcp   Server A (read-only analytics)   scope oga:read

Two auth modes, chosen by environment:

* **OAuth 2.1** (production, works in claude.ai / ChatGPT / Claude Code) — active
  when ``MCP_OAUTH_ISSUER`` is set. The tier is an OAuth *resource server*: it
  validates access tokens from your Authorization Server (a managed provider),
  publishes the RFC 9728 protected-resource metadata, and requires the tier's
  scope. The tier is enabled when its DB DSN env var is set (readonly →
  ``MCP_READONLY_DATABASE_URL``). Needs ``MCP_PUBLIC_URL`` = the service's public
  https URL so token audiences match.

* **Bearer token** (local dev / Claude Code) — active when OAuth is off. The
  tier is enabled + gated by its own ``MCP_*_TOKEN`` bearer token.

Either way the tier connects with its least-privilege DB role from ``roles.sql``;
suspending the one Render service takes it offline (the kill-switch).

Local run:  MCP_READONLY_TOKEN=dev python -m mcp_servers.http_server
Render:     start command ``python -m mcp_servers.http_server`` (binds $PORT)
"""
from __future__ import annotations

import hmac
import importlib
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

# name, bearer-token env, OAuth scope, DB-DSN env (OAuth-mode enablement), module
# Only the read-only tier remains; the tools/admin write tiers were retired.
TIER_DEFS = [
    ("readonly", "MCP_READONLY_TOKEN", "oga:read", "MCP_READONLY_DATABASE_URL",
     "mcp_servers.server_a_readonly"),
]


class BearerAuth:
    """ASGI wrapper: reject any HTTP request whose Authorization header isn't
    exactly ``Bearer <token>`` (constant-time compare)."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = b"Bearer " + token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if not hmac.compare_digest(headers.get(b"authorization", b""), self._expected):
            resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        await self.app(scope, receive, send)


def _health(names):
    async def handler(request):
        return JSONResponse({"status": "ok", "enabled": names,
                             "endpoints": {n: f"/{n}/mcp" for n in names}})
    return handler


def _public_base() -> str:
    return (os.environ.get("MCP_PUBLIC_URL") or "").strip().rstrip("/")


def _transport_security():
    """FastMCP's streamable-HTTP transport has DNS-rebinding protection that
    rejects any Host header not in an allow-list (default: localhost only), so a
    hosted server 421s on its own public hostname. Allow the public host (from
    MCP_PUBLIC_URL, plus any MCP_ALLOWED_HOSTS override). Returns None when no
    public host is set (local/dev), leaving the default localhost protection."""
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    hosts = []
    base = _public_base()
    if base:
        netloc = urlparse(base).netloc
        if netloc:
            hosts += [netloc, f"{netloc}:*"]
    extra = (os.environ.get("MCP_ALLOWED_HOSTS") or "").strip()
    if extra:
        hosts += [h.strip() for h in extra.split(",") if h.strip()]
    if not hosts:
        return None
    origins = [f"https://{h}" for h in hosts if ":*" not in h]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


# ---------------------------------------------------------------------------
# Bearer mode (local / Claude Code) — a tier is on when its token is set.
# ---------------------------------------------------------------------------

def _build_bearer_app() -> Starlette:
    sec = _transport_security()
    tiers = []
    for name, tok_env, _scope, _dsn_env, module_path in TIER_DEFS:
        token = (os.environ.get(tok_env) or "").strip()
        if not token:
            continue
        mcp = importlib.import_module(module_path).build_server(transport_security=sec)
        tiers.append((name, token, mcp))

    names = [n for n, _, _ in tiers]
    routes = [Route("/", _health(names), methods=["GET"]),
              Route("/healthz", _health(names), methods=["GET"])]
    mcps = []
    for name, token, mcp in tiers:
        routes.append(Mount(f"/{name}", app=BearerAuth(mcp.streamable_http_app(), token)))
        mcps.append(mcp)

    @asynccontextmanager
    async def lifespan(_app):
        async with AsyncExitStack() as stack:
            for mcp in mcps:
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)


# ---------------------------------------------------------------------------
# OAuth mode (production) — a tier is on when its DB DSN is set. Each tier is a
# full FastMCP resource-server app (auth middleware + PRM at root paths); we
# dispatch by path so those absolute paths are served unprefixed.
# ---------------------------------------------------------------------------

def _fix_authorization_servers(raw: bytes) -> bytes:
    """Strip the trailing slash from every ``authorization_servers`` entry in a
    protected-resource metadata document.

    RFC 8414/9728: an authorization-server identifier must match the AS's own
    ``issuer`` byte-for-byte. pydantic ``AnyHttpUrl`` appends a trailing slash to a
    host-only issuer (``https://x.authkit.app`` -> ``https://x.authkit.app/``), and
    the MCP SDK emits that value verbatim. AuthKit (and most ASes) advertise the
    issuer WITHOUT the slash, so the mismatch makes MCP clients reject discovery and
    report "automatic client registration isn't supported". Normalising here — the
    one place the metadata leaves the process — keeps DCR/CIMD working."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    servers = data.get("authorization_servers")
    if not isinstance(servers, list):
        return raw
    fixed = [s[:-1] if isinstance(s, str) and s.endswith("/") else s
             for s in servers]
    if fixed == servers:
        return raw
    data["authorization_servers"] = fixed
    return json.dumps(data).encode()


async def _forward_fixing_prm(app, scope, receive, send):
    """Forward the request to ``app`` but rewrite the protected-resource metadata
    JSON on the way out (see ``_fix_authorization_servers``). Buffers the (tiny)
    metadata body so the corrected ``Content-Length`` is sent."""
    state: dict = {"start": None, "body": []}

    async def _send(message):
        if message["type"] == "http.response.start":
            state["start"] = message           # hold until the new length is known
        elif message["type"] == "http.response.body":
            state["body"].append(message.get("body", b""))
            if message.get("more_body"):
                return
            new = _fix_authorization_servers(b"".join(state["body"]))
            start = state["start"]
            start["headers"] = [(k, v) for (k, v) in start.get("headers", [])
                                if k.lower() != b"content-length"]
            start["headers"].append((b"content-length", str(len(new)).encode()))
            await send(start)
            await send({"type": "http.response.body", "body": new,
                        "more_body": False})
        else:
            await send(message)

    await app(scope, receive, _send)


def _build_oauth_app() -> Starlette:
    from mcp_servers.common.oauth import build_auth

    base = _public_base()
    if not base:
        raise RuntimeError("OAuth mode needs MCP_PUBLIC_URL (the service's https URL).")
    sec = _transport_security()

    tiers = []  # (name, mcp, asgi_app)
    for name, _tok_env, _default_scope, dsn_env, module_path in TIER_DEFS:
        if not (os.environ.get(dsn_env) or "").strip():
            continue  # tier stays dark until its DB DSN is configured
        # Per-tier scope is opt-in via env (e.g. MCP_READONLY_SCOPE=oga:read). Empty
        # = any authenticated token, still audience-bound to THIS tier's URL.
        scope = (os.environ.get(f"MCP_{name.upper()}_SCOPE") or "").strip()
        required = [scope] if scope else []
        resource_url = f"{base}/{name}/mcp"
        settings, verifier = build_auth(resource_url, required)
        mcp = importlib.import_module(module_path).build_server(
            auth_settings=settings, token_verifier=verifier,
            http_path=f"/{name}/mcp", transport_security=sec)
        tiers.append((name, mcp, mcp.streamable_http_app()))

    names = [n for n, _, _ in tiers]
    apps = {n: a for n, _, a in tiers}
    # exact paths each tier owns (MCP endpoint + its protected-resource metadata)
    owner = {}
    for n in names:
        owner[f"/{n}/mcp"] = n
        owner[f"/.well-known/oauth-protected-resource/{n}/mcp"] = n

    def _health_response():
        return JSONResponse({"status": "ok", "enabled": names,
                             "endpoints": {n: f"/{n}/mcp" for n in names}})

    async def dispatch(scope, receive, send):
        if scope["type"] != "http":
            return  # lifespan handled by the parent Starlette
        path = scope.get("path", "")
        if path in ("/", "/healthz"):
            return await _health_response()(scope, receive, send)
        # exact owner, or a sub-path of a tier's MCP endpoint (streaming/session)
        name = owner.get(path)
        if name is None:
            for n in names:
                if path.startswith(f"/{n}/mcp/"):
                    name = n
                    break
        if name is None:
            return await JSONResponse({"error": "not found"}, status_code=404)(
                scope, receive, send)
        # Correct the protected-resource metadata's authorization_servers on the
        # way out so its issuer matches the AS byte-for-byte (trailing-slash fix).
        if path.startswith("/.well-known/oauth-protected-resource/"):
            return await _forward_fixing_prm(apps[name], scope, receive, send)
        await apps[name](scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app):
        async with AsyncExitStack() as stack:
            for _n, mcp, _a in tiers:
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    return Starlette(routes=[Mount("/", app=dispatch)], lifespan=lifespan)


def build_app() -> Starlette:
    from mcp_servers.common.oauth import oauth_enabled
    return _build_oauth_app() if oauth_enabled() else _build_bearer_app()


app = build_app()


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
