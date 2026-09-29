"""Token verification against a Keycloak-like JWKS endpoint served over HTTPS by a private CA."""
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

from security.auth import KeycloakTokenVerifier, keycloak_auth, keycloak_http_client

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
def keycloak(tmp_path_factory):
    """A realm's JWKS endpoint on https://localhost, with a certificate from a private CA."""
    folder = tmp_path_factory.mktemp('keycloak')
    ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_cert = _certificate('Test private CA', 'Test private CA', ca_key.public_key(), ca_key, ca=True)
    server_cert = _certificate('localhost', 'Test private CA', server_key.public_key(), ca_key, ca=False)
    (folder / 'ca.crt').write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (folder / 'server.pem').write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM)
        + server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))

    signing_key = jwk.RSAKey.generate_key(2048, parameters={'kid': 'realm-key'})
    jwks = json.dumps({'keys': [signing_key.as_dict(private=False)]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(jwks)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('localhost', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'server.pem')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    realm = f'https://localhost:{server.server_address[1]}/realms/test'
    yield SimpleNamespace(realm=realm, ca=folder / 'ca.crt', key=signing_key)
    server.shutdown()


def _token(keycloak, **claims):
    claims = {'iss': keycloak.realm, 'aud': ['account', AUDIENCE], 'sub': 'u-1', 'azp': 'openwebui',
              'preferred_username': 'john.smith1', 'exp': int(time.time()) + 300, **claims}
    return jwt.encode({'alg': 'RS256', 'kid': 'realm-key'}, claims, keycloak.key)


def _verifier(keycloak, ca, algorithm='RS256'):
    verifier = KeycloakTokenVerifier(jwks_uri=f'{keycloak.realm}/protocol/openid-connect/certs',
                                     issuer=keycloak.realm, audience=AUDIENCE, algorithm=algorithm,
                                     required_scopes=[], http_client=keycloak_http_client(ca))
    verifier.logger = MagicMock()
    return verifier


async def test_token_is_accepted_when_the_private_ca_is_trusted(keycloak):
    access = await _verifier(keycloak, keycloak.ca).load_access_token(_token(keycloak))
    assert access is not None and access.claims['preferred_username'] == 'john.smith1'


async def test_without_the_ca_the_key_fetch_failure_is_logged_as_a_warning(keycloak):
    verifier = _verifier(keycloak, None)
    assert await verifier.load_access_token(_token(keycloak)) is None
    message, *args = verifier.logger.warning.call_args.args
    assert "couldn't get its signing key" in message % tuple(args)
    assert 'CERTIFICATE_VERIFY_FAILED' in str(args[-1])


async def test_a_token_signed_with_another_algorithm_is_logged(keycloak):
    verifier = _verifier(keycloak, keycloak.ca, algorithm='ES256')
    assert await verifier.load_access_token(_token(keycloak)) is None
    warnings = [call.args[0] % call.args[1:] for call in verifier.logger.warning.call_args_list]
    assert any('signed with RS256, but only ES256 is accepted' in w for w in warnings)


async def test_claim_checks_still_apply(keycloak):
    verifier = _verifier(keycloak, keycloak.ca)
    assert await verifier.load_access_token(_token(keycloak, iss='http://keycloak.internal/realms/test')) is None
    assert await verifier.load_access_token(_token(keycloak, aud='account')) is None
    assert await verifier.load_access_token(_token(keycloak, exp=int(time.time()) - 10)) is None


def test_keycloak_auth_uses_the_realm_and_settings():
    settings = SimpleNamespace(keycloak_realm_url='https://kc.example.com/realms/security/',
                               keycloak_audience=AUDIENCE, keycloak_token_algorithm='RS256',
                               public_base_url='https://es-mcp.example.com')
    provider = keycloak_auth(settings, keycloak_http_client(None))
    verifier = provider.token_verifier
    assert isinstance(verifier, KeycloakTokenVerifier)
    assert verifier.jwks_uri == 'https://kc.example.com/realms/security/protocol/openid-connect/certs'
    assert verifier.issuer == 'https://kc.example.com/realms/security'
    assert verifier.audience == AUDIENCE and not verifier.required_scopes
