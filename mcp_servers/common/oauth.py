"""OAuth 2.1 resource-server support for the hosted MCP servers.

Makes a FastMCP server an OAuth Resource Server: it *verifies* access tokens
issued by an external Authorization Server (a managed provider like WorkOS
AuthKit), and never issues them. The SDK, given ``AuthSettings`` + a
``TokenVerifier``, publishes the RFC 9728 protected-resource metadata and the
401 ``WWW-Authenticate`` challenge automatically, so any MCP client (claude.ai,
ChatGPT, Claude Code) can discover the login and connect.

Config (env, names only — set on the Render service):
    MCP_OAUTH_ISSUER     the AS issuer URL, e.g. https://your-tenant.authkit.app
    MCP_OAUTH_JWKS_URI   (optional) the AS JWKS URL; auto-discovered from the
                         issuer's metadata when omitted.

OAuth is "on" whenever MCP_OAUTH_ISSUER is set. When it's unset the servers fall
back to the bearer-token transport (local dev / Claude Code), so nothing here is
required to run locally.

Token checks (the important ones): RS256 signature via the AS JWKS, ``iss`` ==
issuer, ``aud`` == this resource's exact URL (RFC 8707 audience binding — the
single most important check), ``exp``, and the required scope.
"""
from __future__ import annotations

import asyncio
import os

import jwt
from jwt import PyJWKClient
from pydantic import AnyHttpUrl

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

ENV_ISSUER = "MCP_OAUTH_ISSUER"
ENV_JWKS = "MCP_OAUTH_JWKS_URI"


def oauth_enabled() -> bool:
    return bool((os.environ.get(ENV_ISSUER) or "").strip())


def issuer() -> str:
    return (os.environ.get(ENV_ISSUER) or "").strip().rstrip("/")


def resolve_jwks_uri(issuer_url: str) -> str:
    """Explicit ``MCP_OAUTH_JWKS_URI`` if set, else discover it from the AS's
    OAuth/OIDC metadata."""
    explicit = (os.environ.get(ENV_JWKS) or "").strip()
    if explicit:
        return explicit
    import httpx

    for suffix in ("/.well-known/oauth-authorization-server",
                   "/.well-known/openid-configuration"):
        try:
            r = httpx.get(issuer_url + suffix, timeout=10)
            if r.status_code == 200 and r.json().get("jwks_uri"):
                return r.json()["jwks_uri"]
        except Exception:
            continue
    raise RuntimeError(
        f"Could not discover jwks_uri from {issuer_url}. Set {ENV_JWKS} explicitly.")


class JWKSTokenVerifier(TokenVerifier):
    """Verify an AS's RS256 JWT access tokens against its JWKS."""

    def __init__(self, jwks_uri: str, issuer_url: str, audience: str,
                 required_scopes=None):
        self._jwks = PyJWKClient(jwks_uri, cache_keys=True)
        self._issuer = issuer_url
        self._audience = audience
        self._required = set(required_scopes or [])

    def _decode(self, token: str) -> dict:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],                 # never trust the token's alg
            audience=self._audience,              # RFC 8707: aud == this resource
            issuer=self._issuer,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # PyJWKClient does blocking HTTP for the key — keep it off the loop.
            claims = await asyncio.to_thread(self._decode, token)
        except Exception:
            return None
        raw = claims.get("scope") or claims.get("scp") or ""
        scopes = raw.split() if isinstance(raw, str) else list(raw)
        if self._required and not self._required.issubset(scopes):
            return None
        aud = claims.get("aud")
        return AccessToken(
            token=token,
            client_id=claims.get("client_id") or claims.get("azp") or claims.get("sub", ""),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=aud if isinstance(aud, str) else None,
            subject=claims.get("sub"),
            claims=claims,
        )


def build_auth(resource_url: str, required_scopes):
    """Return ``(AuthSettings, TokenVerifier)`` for a resource, or ``(None, None)``
    when OAuth is disabled (no issuer configured)."""
    iss = issuer()
    if not iss:
        return None, None
    jwks_uri = resolve_jwks_uri(iss)
    verifier = JWKSTokenVerifier(jwks_uri, iss, resource_url, required_scopes)
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(iss),
        resource_server_url=AnyHttpUrl(resource_url),
        required_scopes=list(required_scopes),
    )
    return settings, verifier
