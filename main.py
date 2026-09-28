import logging
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from config import Settings
from resources.windows_events import DateRange, get_users, get_top_events_by_user, \
    get_remote_access_events_by_user
from security.audit import AuditMiddleware, configure_audit_log
from security.policy import RbacPolicy
from utils.elasticsearch import ElasticsearchGateway

logger = logging.getLogger(__name__)

settings = Settings()
policy = RbacPolicy.load(settings.rbac_policy_file)
configure_audit_log(settings.audit_log_file)


@asynccontextmanager
async def lifespan(server: FastMCP):
    es = ElasticsearchGateway(settings, policy)
    try:
        yield {'es': es}
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


def _es(ctx: Context) -> ElasticsearchGateway:
    return ctx.lifespan_context['es']


@mcp.tool
async def get_windows_event_users(date_range: DateRange, ctx: Context) -> dict[str, Any]:
    """
    Query Windows events and return all users that feature in those events within the specified time range.
    :param date_range: Period for which to return events
    :return: List of user accounts featuring in events within the specified date range
    """
    return await get_users(_es(ctx), settings.es_windows_index, date_range)


@mcp.tool
async def get_windows_events_by_user(user: str, size: int, date_range: DateRange, ctx: Context) -> dict[str, Any]:
    """
    Return Windows events relating to a specific user.
    :param user: The logon name of the user (e.g., 'john.doe').
    :param size: The maximum number of events to retrieve (e.g., 10, 50, or 100).
    :param date_range: The time period (start and end dates) to filter events. Dates should be in YYYY-MM-DD format.
    :return: Windows events in Elastic Common Schema JSON format
    """
    return await get_top_events_by_user(_es(ctx), settings.es_windows_index, user, size, date_range)


@mcp.tool
async def get_windows_remote_access_events_by_user(user: str, size: int, date_range: DateRange,
                                                   ctx: Context) -> dict[str, Any]:
    """
    Return Microsoft Remote Desktop Gateway (RDG) and Remote Desktop Protocol (RDP) events relating to a specific user.
    :param user: The logon name of the user (e.g., 'john.doe').
    :param size: The maximum number of events to retrieve (e.g., 10, 50, or 100).
    :param date_range: The time period (start and end dates) to filter events in YYYY-MM-DD format.
    :return: Windows events in Elastic Common Schema JSON format
    """
    return await get_remote_access_events_by_user(_es(ctx), settings.es_windows_index, user, size, date_range)


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
