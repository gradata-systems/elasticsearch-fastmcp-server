import logging
from typing import Any, Awaitable, Callable

from elasticsearch import ApiError, AsyncElasticsearch, AuthorizationException, TransportError
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from config import Settings
from security.api_keys import ApiKeyBroker
from security.audit import audit
from security.policy import Caller, RbacPolicy, roles_from_claims

logger = logging.getLogger(__name__)


def gateway_from(ctx: Context) -> 'ElasticsearchGateway':
    return ctx.lifespan_context['es']


def _describe(error: dict[str, Any]) -> str:
    root = (error.get('root_cause') or [error])[0]
    reason = root.get('reason') or error.get('reason') or error.get('type', 'unknown error')
    caused_by = (error.get('caused_by') or {}).get('reason')
    return f"{reason}: {caused_by}" if caused_by and caused_by != reason else reason


def _error_reason(e: ApiError) -> str:
    error = e.body.get('error') if isinstance(e.body, dict) else None
    return _describe(error) if isinstance(error, dict) else str(e)


def shard_failure(body: dict[str, Any]) -> str | None:
    """Reason for the first shard failure in a search response, if any shard failed."""
    shards = body.get('_shards') or {}
    if not shards.get('failed'):
        return None
    failures = shards.get('failures') or [{}]
    return _describe(failures[0].get('reason') or {})


def _hits_summary(body: dict[str, Any]) -> dict[str, Any]:
    hits, shards = body.get('hits', {}), body.get('_shards', {})
    return {'took_ms': body.get('took'), 'hits_returned': len(hits.get('hits', [])), 'hits_total': hits.get('total'),
            'shards_failed': shards.get('failed', 0)}


class ElasticsearchGateway:
    """Runs Elasticsearch requests on behalf of the authenticated MCP caller.

    Every request uses an API key scoped to the caller's permitted indices. The broker
    credentials held by the underlying client are only ever used to mint those keys.
    """

    def __init__(self, settings: Settings, policy: RbacPolicy):
        self.settings = settings
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
        roles = roles_from_claims(token.claims or {}, self.settings.keycloak_roles_client_id)
        return Caller(token.subject, roles, self._policy.index_patterns_for(roles))

    async def _execute(
        self,
        api: str,
        index: str | None,
        request: dict[str, Any],
        call: Callable[[AsyncElasticsearch], Awaitable[Any]],
        summarize: Callable[[dict[str, Any]], dict[str, Any]] = lambda body: {},
    ) -> dict[str, Any]:
        """Run `call` with the caller's scoped client, auditing the outcome.

        `index` is checked against the caller's policy first when given; requests whose
        target can't be checked up front (ES|QL) rely on Elasticsearch alone.
        """
        caller = self.current_caller()
        who = {'api': api, 'roles': sorted(caller.roles), 'index': index}
        if not caller.index_patterns:
            audit('access_denied', reason='no_mapped_roles', **who)
            raise ToolError("Your roles do not grant access to any data")
        if index is not None and not caller.may_read(index):
            audit('access_denied', reason='index_not_in_policy', **who)
            raise ToolError(f"Access to index '{index}' is not permitted for your roles")

        api_key = await self._broker.key_for(caller.index_patterns)
        client = self._client.options(api_key=api_key, opaque_id=f'mcp:{caller.subject}')
        logger.debug("%s on %s: %s", api, index, request)
        try:
            response = await call(client)
        except AuthorizationException as e:
            audit('access_denied', reason='elasticsearch_403', request=request, **who)
            raise ToolError("Elasticsearch denied access to the requested data") from e
        except ApiError as e:
            reason = _error_reason(e)
            audit('es_request', outcome='error', error=reason, request=request, **who)
            raise ToolError(f"Elasticsearch rejected the request: {reason}") from e
        except TransportError as e:
            logger.exception("Elasticsearch %s on %s failed", api, index)
            audit('es_request', outcome='error', error=str(e), request=request, **who)
            raise ToolError("Elasticsearch is unavailable") from e

        body = response.body
        shards = body.get('_shards') or {}
        if shards.get('failed') and shards['failed'] >= shards.get('total', 0) - shards.get('skipped', 0):
            # Every shard that ran the query failed, so the (empty) result is meaningless.
            reason = shard_failure(body)
            audit('es_request', outcome='error', error=reason, request=request, **who)
            raise ToolError(f"Elasticsearch rejected the request: {reason}")
        outcome = 'partial' if shards.get('failed') else 'success'
        audit('es_request', outcome=outcome, request=request, **summarize(body), **who)
        return body

    async def search(self, index: str, **params: Any) -> dict[str, Any]:
        if 'size' in params:
            params['size'] = max(0, min(params['size'], self.settings.max_result_size))
        return await self._execute('search', index, params,
                                   lambda c: c.search(index=index, **params), _hits_summary)

    async def field_caps(self, index: str, fields: list[str]) -> dict[str, Any]:
        return await self._execute('field_caps', index, {'fields': fields},
                                   lambda c: c.field_caps(index=index, fields=fields),
                                   lambda body: {'fields_returned': len(body.get('fields', {}))})

    async def resolve_accessible(self) -> dict[str, Any]:
        """Indices, aliases and data streams matching the caller's policy patterns.

        Wildcards are resolved by Elasticsearch under the caller's scoped key, so only
        targets the caller can actually read are returned.
        """
        caller = self.current_caller()
        names = sorted(caller.index_patterns)
        return await self._execute('resolve_index', None, {'names': names},
                                   lambda c: c.indices.resolve_index(name=names, ignore_unavailable=True,
                                                                     allow_no_indices=True))

    async def esql(self, query: str, filter: dict[str, Any]) -> dict[str, Any]:
        return await self._execute('esql', None, {'query': query, 'filter': filter},
                                   lambda c: c.esql.query(query=query, filter=filter),
                                   lambda body: {'took_ms': body.get('took'), 'rows': len(body.get('values', []))})
