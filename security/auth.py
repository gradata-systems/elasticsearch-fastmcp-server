"""Verifying access tokens from an OpenID Connect provider.

Tokens are JWTs checked against the provider's signing keys. The keys come from the JWKS URI in the
provider's discovery document, unless one is configured. FastMCP's JWT verifier fetches keys with
the system CAs only, and logs why a token was rejected at debug level when the keys can't be
fetched or the signing algorithm is wrong, so those failures show up only as "invalid token". This
verifier trusts an extra CA for the provider and logs those failures as warnings.
"""
import ssl
from pathlib import Path
from typing import Any

import httpx2
from fastmcp.server.auth import AccessToken, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.utilities.auth import decode_jwt_header
from pydantic import AnyHttpUrl

from config import Settings


def discovery_url(issuer: str) -> str:
    return issuer.rstrip('/') + '/.well-known/openid-configuration'


class OIDCTokenVerifier(JWTVerifier):
    """JWTVerifier that finds the JWKS URI by OIDC discovery when none is given, and reports signing
    key and algorithm failures, not just claim mismatches."""

    def __init__(self, *, issuer: str, jwks_uri: str | None, http_client: httpx2.AsyncClient, **kwargs: Any):
        # Discovery happens on the first key fetch, so the server starts even while the provider is down.
        super().__init__(issuer=issuer, jwks_uri=jwks_uri or discovery_url(issuer), http_client=http_client,
                         **kwargs)
        self._client = http_client
        self._discover = jwks_uri is None

    async def _fetch_jwks(self) -> dict[str, Any]:
        if self._discover:
            response = await self._client.get(self.jwks_uri)
            response.raise_for_status()
            metadata = response.json()
            if metadata.get('issuer') != self.issuer:
                raise ValueError(f"discovery document's issuer is {metadata.get('issuer')!r}, "
                                 f"expected {self.issuer!r} (ES_MCP_OIDC_ISSUER)")
            if not metadata.get('jwks_uri'):
                raise ValueError('discovery document has no jwks_uri')
            self.jwks_uri, self._discover = metadata['jwks_uri'], False
        return await super()._fetch_jwks()

    async def _get_verification_key(self, token: str) -> str | bytes:
        try:
            return await super()._get_verification_key(token)
        except Exception as e:
            self.logger.warning("Bearer token rejected: couldn't get its signing key from %s: %s", self.jwks_uri, e)
            raise

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            algorithm = decode_jwt_header(token).get('alg')
        except Exception:
            algorithm = None
        if algorithm and algorithm != self.algorithm:
            self.logger.warning("Bearer token rejected: signed with %s, but only %s is accepted "
                                "(ES_MCP_OIDC_TOKEN_ALGORITHM)", algorithm, self.algorithm)
        return await super().load_access_token(token)


def oidc_http_client(ca_file: Path | None) -> httpx2.AsyncClient:
    """HTTPS client for the OIDC provider that trusts the system CAs plus `ca_file`, if given."""
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=str(ca_file))
    return httpx2.AsyncClient(verify=context, timeout=httpx2.Timeout(10.0))


def oidc_auth(settings: Settings, http_client: httpx2.AsyncClient) -> RemoteAuthProvider:
    """Resource server auth: verifies the provider's tokens and advertises the provider to clients
    in the protected resource metadata (RFC 9728)."""
    verifier: Any = OIDCTokenVerifier(
        issuer=settings.oidc_issuer,
        jwks_uri=settings.oidc_jwks_uri,
        audience=settings.oidc_audience,
        algorithm=settings.oidc_token_algorithm,
        # FastMCP's providers often require 'openid', which client-credentials agents only get when
        # they ask for it. Access rests on the issuer, the audience and the username claim instead.
        required_scopes=[],
        http_client=http_client,
    )
    return RemoteAuthProvider(token_verifier=verifier, authorization_servers=[AnyHttpUrl(settings.oidc_issuer)],
                              base_url=settings.public_base_url.rstrip('/'))
