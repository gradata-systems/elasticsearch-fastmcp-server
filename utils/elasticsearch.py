import logging
from typing import Any

from elasticsearch import ApiError, AsyncElasticsearch, AuthorizationException, TransportError
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from config import Settings
from security.api_keys import ApiKeyBroker
from security.audit import audit
from security.policy import Caller, RbacPolicy, roles_from_claims

logger = logging.getLogger(__name__)


class ElasticsearchGateway:
    """Runs Elasticsearch requests on behalf of the authenticated MCP caller.

    Every request uses an API key scoped to the caller's permitted indices. The broker
    credentials held by the underlying client are only ever used to mint those keys.
    """

    def __init__(self, settings: Settings, policy: RbacPolicy):
        self._settings = settings
        self._policy = policy
        self._client = AsyncElasticsearch(
            settings.es_url,
            basic_auth=(settings.es_broker_username, settings.es_broker_password.get_secret_value()),
            ca_certs=str(settings.es_ca_certs) if settings.es_ca_certs else None,
            verify_certs=True,
            request_timeout=settings.es_request_timeout,
        )
        self._broker = ApiKeyBroker(self._client, lifetime_seconds=settings.api_key_lifetime_minutes * 60)

    async def close(self) -> None:
        await self._client.close()

    def current_caller(self) -> Caller:
        token = get_access_token()
        if token is None or not token.subject:
            audit('access_denied', reason='unauthenticated')
            raise ToolError("Not authenticated")
        roles = roles_from_claims(token.claims or {}, self._settings.keycloak_roles_client_id)
        return Caller(token.subject, roles, self._policy.index_patterns_for(roles))

    async def search(self, index: str, **params: Any) -> dict[str, Any]:
        caller = self.current_caller()
        who = {'roles': sorted(caller.roles), 'index': index}
        if not caller.may_read(index):
            audit('access_denied', reason='index_not_in_policy', **who)
            raise ToolError(f"Access to index '{index}' is not permitted for your roles")

        if 'size' in params:
            params['size'] = max(0, min(params['size'], self._settings.max_result_size))

        api_key = await self._broker.key_for(caller.index_patterns)
        client = self._client.options(api_key=api_key, opaque_id=f'mcp:{caller.subject}')
        logger.debug("Search on %s: %s", index, params)
        try:
            response = await client.search(index=index, **params)
        except AuthorizationException as e:
            audit('access_denied', reason='elasticsearch_403', request=params, **who)
            raise ToolError(f"Elasticsearch denied access to index '{index}'") from e
        except (ApiError, TransportError) as e:
            logger.exception("Elasticsearch search on %s failed", index)
            audit('es_search', outcome='error', error=str(e), request=params, **who)
            raise ToolError(f"Elasticsearch query failed: {e}") from e

        body = response.body
        audit('es_search', outcome='success', request=params, took_ms=body.get('took'),
              hits_returned=len(body.get('hits', {}).get('hits', [])),
              hits_total=body.get('hits', {}).get('total'), **who)
        return body
