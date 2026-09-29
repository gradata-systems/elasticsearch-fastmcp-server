"""Token verification against an OIDC provider served over HTTPS by a private CA."""
import datetime as dt
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from joserfc import jwk, jwt

from security.auth import OIDCTokenVerifier, oidc_auth, oidc_http_client

AUDIENCE = 'es-mcp'


def _certificate(subject, issuer_name, public_key, signing_key, *, ca: bool):
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
               .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
               .public_key(public_key).serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(hours=1))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
    return builder.sign(signing_key, hashes.SHA256())


@pytest.fixture(scope='module')
def idp(tmp_path_factory):
    """An OIDC provider's discovery document and JWKS on https://localhost, with a certificate from a
    private CA."""
    folder = tmp_path_factory.mktemp('idp')
    ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_cert = _certificate('Test private CA', 'Test private CA', ca_key.public_key(), ca_key, ca=True)
    server_cert = _certificate('localhost', 'Test private CA', server_key.public_key(), ca_key, ca=False)
    (folder / 'ca.crt').write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (folder / 'server.pem').write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM)
        + server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))

    signing_key = jwk.RSAKey.generate_key(2048, parameters={'kid': 'signing-key'})
    documents = {'/certs': {'keys': [signing_key.as_dict(private=False)]}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            document = documents.get(self.path)
            self.send_response(200 if document else 404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(document or {}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('localhost', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'server.pem')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'https://localhost:{server.server_address[1]}'
    documents['/.well-known/openid-configuration'] = {'issuer': base, 'jwks_uri': f'{base}/certs'}
    yield SimpleNamespace(issuer=base, jwks_uri=f'{base}/certs', ca=folder / 'ca.crt', key=signing_key,
                          documents=documents)
    server.shutdown()


def _token(idp, **claims):
    claims = {'iss': idp.issuer, 'aud': ['account', AUDIENCE], 'sub': 'u-1', 'azp': 'openwebui',
              'preferred_username': 'john.smith1', 'exp': int(time.time()) + 300, **claims}
    return jwt.encode({'alg': 'RS256', 'kid': 'signing-key'}, claims, idp.key)


def _verifier(idp, ca, algorithm='RS256', jwks_uri=None, issuer=None):
    verifier = OIDCTokenVerifier(issuer=issuer or idp.issuer, jwks_uri=jwks_uri, audience=AUDIENCE,
                                 algorithm=algorithm, required_scopes=[], http_client=oidc_http_client(ca))
    verifier.logger = MagicMock()
    return verifier


def _warnings(verifier):
    return [call.args[0] % call.args[1:] for call in verifier.logger.warning.call_args_list]


async def test_the_signing_keys_are_found_by_discovery(idp):
    verifier = _verifier(idp, idp.ca)
    access = await verifier.load_access_token(_token(idp))
    assert access is not None and access.claims['preferred_username'] == 'john.smith1'
    assert verifier.jwks_uri == idp.jwks_uri


async def test_a_configured_jwks_uri_skips_discovery(idp):
    verifier = _verifier(idp, idp.ca, jwks_uri=idp.jwks_uri)
    assert await verifier.load_access_token(_token(idp)) is not None


async def test_a_discovery_document_for_another_issuer_is_refused(idp):
    issuer = f'{idp.issuer}/other'
    idp.documents['/other/.well-known/openid-configuration'] = {'issuer': idp.issuer, 'jwks_uri': idp.jwks_uri}
    verifier = _verifier(idp, idp.ca, issuer=issuer)
    assert await verifier.load_access_token(_token(idp, iss=issuer)) is None
    assert any("discovery document's issuer is" in w for w in _warnings(verifier))


async def test_without_the_ca_the_key_fetch_failure_is_logged_as_a_warning(idp):
    verifier = _verifier(idp, None)
    assert await verifier.load_access_token(_token(idp)) is None
    message, *args = verifier.logger.warning.call_args.args
    assert "couldn't get its signing key" in message % tuple(args)
    assert 'CERTIFICATE_VERIFY_FAILED' in str(args[-1])


async def test_a_token_signed_with_another_algorithm_is_logged(idp):
    verifier = _verifier(idp, idp.ca, algorithm='ES256')
    assert await verifier.load_access_token(_token(idp)) is None
    assert any('signed with RS256, but only ES256 is accepted' in w for w in _warnings(verifier))


async def test_claim_checks_still_apply(idp):
    verifier = _verifier(idp, idp.ca)
    assert await verifier.load_access_token(_token(idp, iss='http://idp.internal')) is None
    assert await verifier.load_access_token(_token(idp, aud='account')) is None
    assert await verifier.load_access_token(_token(idp, exp=int(time.time()) - 10)) is None


def test_oidc_auth_uses_the_settings_and_advertises_the_issuer():
    settings = SimpleNamespace(oidc_issuer='https://idp.example.com/realms/security', oidc_jwks_uri=None,
                               oidc_audience=AUDIENCE, oidc_token_algorithm='RS256',
                               public_base_url='https://es-mcp.example.com')
    provider = oidc_auth(settings, oidc_http_client(None))
    verifier = provider.token_verifier
    assert isinstance(verifier, OIDCTokenVerifier)
    assert verifier.jwks_uri == 'https://idp.example.com/realms/security/.well-known/openid-configuration'
    assert verifier.issuer == 'https://idp.example.com/realms/security'
    assert verifier.audience == AUDIENCE and not verifier.required_scopes
    assert [str(url) for url in provider.authorization_servers] == ['https://idp.example.com/realms/security']
