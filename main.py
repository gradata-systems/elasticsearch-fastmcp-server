import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from config import Settings
from security.audit import AuditMiddleware, configure_audit_log
from security.policy import AccessPolicy, Caller
from sources.packs import SourceTool, exposed_packs, load_packs
from tools import generic
from utils.elasticsearch import ElasticsearchGateway

logger = logging.getLogger(__name__)

settings = Settings()
policy = AccessPolicy.load(settings.access_policy_file)
configure_audit_log(settings.audit_log_file)
exposed = Caller('', '', frozenset(policy.exposed_indices))
packs = exposed_packs(load_packs(settings.packs_dir), exposed.may_read)


@asynccontextmanager
async def lifespan(server: FastMCP):
    es = ElasticsearchGateway(settings, policy)
    try:
        yield {'es': es, 'packs': packs}
    finally:
        await es.close()


mcp = FastMCP(
    "elasticsearch",
    lifespan=lifespan,
    auth=KeycloakAuthProvider(
        realm_url=settings.keycloak_realm_url,
        base_url=settings.public_base_url,
        audience=settings.keycloak_audience,
    ),
    middleware=[AuditMiddleware()],
)

for tool in generic.ALL_TOOLS:
    mcp.tool(tool)
for pack in packs:
    for spec in pack.tools:
        mcp.add_tool(SourceTool.build(pack, spec))


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
