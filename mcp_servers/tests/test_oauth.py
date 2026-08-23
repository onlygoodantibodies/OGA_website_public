"""OAuth resource-server behaviour, proven against a MOCK authorization server.

Spins up a tiny AS (real RSA keypair; serves OAuth metadata + JWKS), builds a
FastMCP resource server via common.oauth.build_auth, and checks:
  - no token            -> 401 with a WWW-Authenticate: ... resource_metadata=...
  - the protected-resource metadata is served as JSON, naming the AS
  - a valid token       -> gets past auth (not 401)
  - wrong audience / expired / missing scope -> rejected
  - the verifier unit accepts good tokens and rejects bad ones

This is all in-process (no WorkOS, no network beyond a localhost JWKS server).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

KID = "test-key"
SCOPE = "oga:read"


# ── a mock Authorization Server (metadata + JWKS) ───────────────────────────

class _MockAS:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
        self._jwks = {"keys": [jwk]}
        self._server = None
        self.issuer = None

    def start(self):
        jwks = self._jwks
        holder = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/jwks":
                    body = jwks
                elif self.path in ("/.well-known/oauth-authorization-server",
                                   "/.well-known/openid-configuration"):
                    body = {"issuer": holder["issuer"],
                            "jwks_uri": holder["issuer"] + "/jwks",
                            "authorization_endpoint": holder["issuer"] + "/authorize",
                            "token_endpoint": holder["issuer"] + "/token",
                            "registration_endpoint": holder["issuer"] + "/register"}
                else:
                    self.send_response(404); self.end_headers(); return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = HTTPServer(("127.0.0.1", 0), H)
        port = self._server.server_address[1]
        self.issuer = f"http://127.0.0.1:{port}"
        holder["issuer"] = self.issuer
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()

    def mint(self, *, aud, scope=SCOPE, exp_delta=3600, iss=None):
        now = int(time.time())
        claims = {"iss": iss or self.issuer, "aud": aud, "sub": "user-123",
                  "iat": now, "exp": now + exp_delta, "scope": scope,
                  "client_id": "test-client"}
        pem = self.key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": KID})


@pytest.fixture()
def mock_as(monkeypatch):
    as_ = _MockAS().start()
    monkeypatch.setenv("MCP_OAUTH_ISSUER", as_.issuer)
    monkeypatch.setenv("MCP_OAUTH_JWKS_URI", as_.issuer + "/jwks")
    try:
        yield as_
    finally:
        as_.stop()


RESOURCE = "http://testserver/oga/mcp"


def _build_app():
    from mcp.server.fastmcp import FastMCP
    from mcp_servers.common.oauth import build_auth

    settings, verifier = build_auth(RESOURCE, [SCOPE])
    mcp = FastMCP("oauth-test", streamable_http_path="/oga/mcp",
                  auth=settings, token_verifier=verifier)

    @mcp.tool()
    def ping() -> str:
        return "pong"

    return mcp


# ── verifier unit ───────────────────────────────────────────────────────────

def test_verifier_accepts_valid_and_rejects_bad(mock_as):
    import asyncio
    from mcp_servers.common.oauth import build_auth

    _, verifier = build_auth(RESOURCE, [SCOPE])

    good = mock_as.mint(aud=RESOURCE)
    tok = asyncio.run(verifier.verify_token(good))
    assert tok is not None and SCOPE in tok.scopes and tok.subject == "user-123"

    assert asyncio.run(verifier.verify_token(mock_as.mint(aud="http://evil/mcp"))) is None
    assert asyncio.run(verifier.verify_token(mock_as.mint(aud=RESOURCE, exp_delta=-30))) is None
    assert asyncio.run(verifier.verify_token(mock_as.mint(aud=RESOURCE, scope="other:scope"))) is None
    assert asyncio.run(verifier.verify_token("not-a-jwt")) is None


# ── full resource-server HTTP behaviour ─────────────────────────────────────

async def _drive(mock_as):
    mcp = _build_app()
    app = mcp.streamable_http_app()
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            results = {}
            # 1. no token -> 401 + challenge pointing at the PRM
            r = await c.post("/oga/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
            results["no_token_status"] = r.status_code
            results["challenge"] = r.headers.get("www-authenticate", "")
            # 2. protected-resource metadata as JSON
            r = await c.get("/.well-known/oauth-protected-resource/oga/mcp")
            results["prm_status"] = r.status_code
            results["prm"] = r.json() if r.status_code == 200 else {}
            # 3. valid token -> not 401
            good = mock_as.mint(aud=RESOURCE)
            r = await c.post("/oga/mcp", headers={"Authorization": f"Bearer {good}"},
                             json={"jsonrpc": "2.0", "method": "ping", "id": 1})
            results["valid_status"] = r.status_code
            # 4. bad tokens -> 401
            bad = mock_as.mint(aud="http://evil/mcp")
            r = await c.post("/oga/mcp", headers={"Authorization": f"Bearer {bad}"},
                             json={"jsonrpc": "2.0", "method": "ping", "id": 1})
            results["wrong_aud_status"] = r.status_code
            return results


def test_resource_server_http_flow(mock_as):
    import asyncio
    r = asyncio.run(_drive(mock_as))
    assert r["no_token_status"] == 401
    assert "resource_metadata" in r["challenge"]
    assert r["prm_status"] == 200
    assert mock_as.issuer in json.dumps(r["prm"]["authorization_servers"])
    assert r["prm"]["resource"].rstrip("/") == RESOURCE
    assert r["valid_status"] != 401          # auth passed
    assert r["wrong_aud_status"] == 401      # audience binding enforced


# ── the http_server (readonly-only) in OAuth mode ───────────────────────────

BASE = "http://testserver"


async def _drive_http_server(mock_as):
    from mcp_servers import http_server
    app = http_server.build_app()          # rebuilt with the OAuth env now set

    from asgi_lifespan import LifespanManager  # noqa
    results = {}
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            results["health"] = (await c.get("/healthz")).json()
            # readonly: no token -> 401; PRM present
            results["ro_no_token"] = (await c.post("/readonly/mcp", json={})).status_code
            results["ro_prm"] = (await c.get(
                "/.well-known/oauth-protected-resource/readonly/mcp")).json()
            # readonly: valid read token -> not 401
            ro_tok = mock_as.mint(aud=f"{BASE}/readonly/mcp", scope="oga:read")
            results["ro_valid"] = (await c.post(
                "/readonly/mcp", headers={"Authorization": f"Bearer {ro_tok}"},
                json={"jsonrpc": "2.0", "method": "ping", "id": 1})).status_code
            # a readonly token minted for another audience must NOT be accepted
            wrong_tok = mock_as.mint(aud=f"{BASE}/other/mcp", scope="oga:read")
            results["ro_wrong_aud"] = (await c.post(
                "/readonly/mcp", headers={"Authorization": f"Bearer {wrong_tok}"},
                json={})).status_code
            # the retired write tiers are gone -> 404
            results["tools_dark"] = (await c.post("/tools/mcp", json={})).status_code
            results["admin_dark"] = (await c.post("/admin/mcp", json={})).status_code
    return results


def test_http_server_oauth_readonly(mock_as, monkeypatch):
    import asyncio
    monkeypatch.setenv("MCP_PUBLIC_URL", BASE)
    # only the readonly tier remains (DSN points at the test db via conftest).
    monkeypatch.delenv("PIPELINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("MCP_ADMIN_DATABASE_URL", raising=False)
    # exercise scope enforcement (empty by default in production)
    monkeypatch.setenv("MCP_READONLY_SCOPE", "oga:read")

    r = asyncio.run(_drive_http_server(mock_as))
    assert r["health"]["enabled"] == ["readonly"]
    assert r["ro_no_token"] == 401
    assert r["ro_prm"]["resource"].rstrip("/") == f"{BASE}/readonly/mcp"
    # The advertised authorization server must EXACTLY equal the AS's issuer —
    # NO trailing slash. pydantic AnyHttpUrl appends one; AuthKit advertises the
    # issuer without it, and a mismatch makes clients reject OAuth discovery
    # ("automatic client registration isn't supported"). Byte-for-byte match.
    assert r["ro_prm"]["authorization_servers"] == [mock_as.issuer]
    assert r["ro_valid"] != 401
    assert r["ro_wrong_aud"] == 401          # audience binding enforced
    assert r["tools_dark"] == 404            # retired write tiers stay dark
    assert r["admin_dark"] == 404


# ── the DNS-rebinding Host allow-list (the 421 bug from the first deploy) ────

PUBLIC = "https://oga-mcp.onrender.com"


async def _initialize(mock_as, host_header=None):
    from asgi_lifespan import LifespanManager
    from mcp_servers import http_server

    app = http_server.build_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as c:
            tok = mock_as.mint(aud=f"{PUBLIC}/readonly/mcp")
            headers = {"Authorization": f"Bearer {tok}",
                       "Accept": "application/json, text/event-stream",
                       "Content-Type": "application/json"}
            if host_header:
                headers["Host"] = host_header
            body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "1"}}}
            r = await c.post("/readonly/mcp", json=body, headers=headers)
            return r.status_code


def _readonly_only(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.delenv("PIPELINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("MCP_ADMIN_DATABASE_URL", raising=False)


def test_public_host_is_accepted(mock_as, monkeypatch):
    # A valid token on the real public hostname must get PAST the host check and
    # complete initialize (200) — this is the exact path that 421'd on deploy.
    import asyncio
    _readonly_only(monkeypatch)
    assert asyncio.run(_initialize(mock_as)) == 200


def test_foreign_host_is_rejected(mock_as, monkeypatch):
    # A Host header not on the allow-list is still refused (DNS-rebinding guard).
    import asyncio
    _readonly_only(monkeypatch)
    assert asyncio.run(_initialize(mock_as, host_header="evil.example.com")) == 421
