import logging
from typing import Any, Awaitable, Callable

from elasticsearch import ApiError, AsyncElasticsearch, AuthenticationException, AuthorizationException,     TransportError
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from config import Settings
from security.audit import audit
from security.esql import UnsupportedQuery, source_indices
from security.policy import AccessPolicy, Caller

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
    """Runs Elasticsearch requests as the authenticated MCP caller.

    The server logs in as an impersonation account and sends every request with
    `es-security-runas-user` set to the caller's username, so Elasticsearch applies that
    user's own roles. The impersonation account has no index privileges of its own.
    """

    RUN_AS_HEADER = 'es-security-runas-user'

    def __init__(self, settings: Settings, policy: AccessPolicy):
        self.settings = settings
        self._policy = policy
        self._client = AsyncElasticsearch(
            settings.es_url,
            basic_auth=(settings.es_impersonator_username, settings.es_impersonator_password.get_secret_value()),
            ca_certs=str(settings.es_ca_certs) if settings.es_ca_certs else None,
            verify_certs=True,
            request_timeout=settings.es_request_timeout,
        )

    async def close(self) -> None:
        await self._client.close()

    def current_caller(self) -> Caller:
        token = get_access_token()
        if token is None or not token.subject:
            audit('access_denied', reason='unauthenticated')
            raise ToolError("Not authenticated")
        username = (token.claims or {}).get(self.settings.username_claim)
        if not isinstance(username, str) or not self._policy.may_impersonate(
                username, self.settings.es_impersonator_username):
            audit('access_denied', reason='username_not_impersonable', es_user=username)
            raise ToolError("Your account is not permitted to query Elasticsearch through this server")
        return Caller(token.subject, username, frozenset(self._policy.exposed_indices))

    async def _execute(
        self,
        api: str,
        index: str | None,
        request: dict[str, Any],
        call: Callable[[AsyncElasticsearch], Awaitable[Any]],
        summarize: Callable[[dict[str, Any]], dict[str, Any]] = lambda body: {},
    ) -> dict[str, Any]:
        """Run `call` as the caller, auditing the outcome.

        `index` (comma-separated targets) is checked against the exposed patterns first;
        Elasticsearch then enforces the caller's own privileges.
        """
        caller = self.current_caller()
        who = {'api': api, 'es_user': caller.username, 'index': index}
        if index is not None and not caller.may_read(index):
            audit('access_denied', reason='index_not_exposed', **who)
            raise ToolError(f"Index '{index}' is not available through this server; see list_data_sources")

        client = self._client.options(headers={self.RUN_AS_HEADER: caller.username},
                                      opaque_id=f'mcp:{caller.username}')
        logger.debug("%s on %s as %s: %s", api, index, caller.username, request)
        try:
            response = await call(client)
        except (AuthenticationException, AuthorizationException) as e:
            run_as_failed = e.status_code == 401 or 'unauthorized to run as' in str(e)
            audit('access_denied', reason='run_as_denied' if run_as_failed else 'elasticsearch_403',
                  request=request, **who)
            if run_as_failed:
                raise ToolError(f"Elasticsearch has no usable account '{caller.username}' to run this as") from e
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
        """Indices, aliases and data streams matching the exposed patterns.

        Wildcards are resolved by Elasticsearch as the caller, so only targets the caller
        can actually read are returned.
        """
        names = sorted(self._policy.exposed_indices)
        return await self._execute('resolve_index', None, {'names': names},
                                   lambda c: c.indices.resolve_index(name=names, ignore_unavailable=True,
                                                                     allow_no_indices=True))

    async def esql(self, query: str, filter: dict[str, Any]) -> dict[str, Any]:
        try:
            indices = ','.join(source_indices(query))
        except UnsupportedQuery as e:
            raise ToolError(str(e)) from e
        return await self._execute('esql', indices, {'query': query, 'filter': filter},
                                   lambda c: c.esql.query(query=query, filter=filter),
                                   lambda body: {'took_ms': body.get('took'), 'rows': len(body.get('values', []))})
