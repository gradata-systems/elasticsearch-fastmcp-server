import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from config import Settings
from security.audit import AuditMiddleware, configure_audit_log
from security.auth import oidc_auth, oidc_http_client
from security.policy import AccessPolicy, Caller
from sources.packs import exposed_packs, load_packs
from tools import deployment
from utils.call_limits import CallLimitMiddleware
from utils.elasticsearch import ElasticsearchGateway

logger = logging.getLogger(__name__)

settings = Settings()
policy = AccessPolicy.load(settings.access_policy_file)
configure_audit_log(settings.audit_log_file, settings.cluster_name)
exposed = Caller('', '', frozenset(policy.exposed_indices))
packs = exposed_packs(load_packs(settings.packs_dir), exposed.may_read) if settings.packs_dir else []


@asynccontextmanager
async def lifespan(server: FastMCP):
    es = ElasticsearchGateway(settings, policy)
    try:
        yield {'es': es, 'packs': packs}
    finally:
        await es.close()


mcp = FastMCP(
    settings.cluster_name or "elasticsearch",
    instructions=deployment.server_instructions(settings, packs),
    lifespan=lifespan,
    # The HTTP client lives as long as the process; it fetches the OIDC provider's signing keys.
    auth=oidc_auth(settings, oidc_http_client(settings.oidc_ca_certs)),
    middleware=[AuditMiddleware(), CallLimitMiddleware(settings)],
)

@mcp.custom_route('/healthz', methods=['GET'], include_in_schema=False)
async def healthz(request: Request) -> Response:
    """Liveness and readiness probe. Unauthenticated, and deliberately independent of Elasticsearch
    and the OIDC provider so an outage there doesn't restart every replica."""
    return PlainTextResponse('ok')


deployment.register(mcp, settings, packs)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn_config = {}
    if settings.tls_certfile and settings.tls_keyfile:
        uvicorn_config = {'ssl_certfile': str(settings.tls_certfile), 'ssl_keyfile': str(settings.tls_keyfile)}
    else:
        logger.warning("TLS not configured; only run like this behind a TLS-terminating proxy")
    mcp.run(
        transport='http',
        host=settings.host,
        port=settings.port,
        uvicorn_config=uvicorn_config,
    )
