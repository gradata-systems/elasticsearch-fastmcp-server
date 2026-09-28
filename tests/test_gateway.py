from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken

from config import Settings
from security.policy import RbacPolicy
from utils.elasticsearch import ElasticsearchGateway

SETTINGS = Settings(
    _env_file=None,
    es_url='https://es.example:9200',
    es_broker_username='broker',
    es_broker_password='secret',
    keycloak_realm_url='https://kc.example/realms/r',
    keycloak_audience='es-mcp',
    keycloak_roles_client_id='es-mcp',
    public_base_url='https://mcp.example',
    max_result_size=100,
)
POLICY = RbacPolicy.model_validate({'roles': {'helpdesk': {'indices': ['ecs-microsoft-windows-*']}}})


def token(roles):
    return AccessToken(token='t', client_id='c', scopes=[], subject='alice',
                       claims={'resource_access': {'es-mcp': {'roles': roles}}})


@pytest.fixture
def gateway():
    gw = ElasticsearchGateway(SETTINGS, POLICY)
    gw._broker.key_for = AsyncMock(return_value='scoped-key')
    scoped = MagicMock()
    scoped.search = AsyncMock(return_value=MagicMock(body={'hits': {'hits': []}}))
    gw._client = MagicMock()
    gw._client.options.return_value = scoped
    return gw


async def test_unauthenticated_is_rejected(gateway):
    with patch('utils.elasticsearch.get_access_token', return_value=None):
        with pytest.raises(ToolError, match='Not authenticated'):
            await gateway.search('ecs-microsoft-windows-v1', size=10)
    gateway._broker.key_for.assert_not_called()


async def test_index_outside_policy_is_rejected_before_es(gateway):
    with patch('utils.elasticsearch.get_access_token', return_value=token(['helpdesk'])), \
            patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match='not permitted'):
            await gateway.search('ecs-nginx-v1', size=10)
    gateway._client.options.assert_not_called()
    audit.assert_called_once_with('access_denied', reason='index_not_in_policy', api='search', roles=['helpdesk'],
                                  index='ecs-nginx-v1')


async def test_caller_without_mapped_roles_is_rejected(gateway):
    with patch('utils.elasticsearch.get_access_token', return_value=token(['something-else'])):
        with pytest.raises(ToolError):
            await gateway.search('ecs-microsoft-windows-v1', size=10)
    gateway._broker.key_for.assert_not_called()


async def test_search_uses_scoped_key_and_caps_size(gateway):
    with patch('utils.elasticsearch.get_access_token', return_value=token(['helpdesk'])):
        with patch('utils.elasticsearch.audit') as audit:
            await gateway.search('ecs-microsoft-windows-v1', size=10_000, query={'match_all': {}})
        event, fields = audit.call_args.args[0], audit.call_args.kwargs
        assert event == 'es_request' and fields['api'] == 'search' and fields['outcome'] == 'success'
        assert fields['request'] == {'size': 100, 'query': {'match_all': {}}}

    gateway._broker.key_for.assert_awaited_once_with(frozenset({'ecs-microsoft-windows-*'}))
    gateway._client.options.assert_called_once_with(api_key='scoped-key', opaque_id='mcp:alice')
    gateway._client.options.return_value.search.assert_awaited_once_with(
        index='ecs-microsoft-windows-v1', size=100, query={'match_all': {}})


async def test_es_error_reason_is_surfaced(gateway):
    from elasticsearch import BadRequestError
    meta = MagicMock(status=400)
    error = BadRequestError('bad', meta, {'error': {
        'root_cause': [{'reason': 'Failed to parse query [a:(]'}], 'caused_by': {'reason': 'Encountered EOF'}}})
    gateway._client.options.return_value.search = AsyncMock(side_effect=error)
    with patch('utils.elasticsearch.get_access_token', return_value=token(['helpdesk'])), \
            patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match=r'Failed to parse query \[a:\(\]: Encountered EOF'):
            await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert audit.call_args.kwargs['outcome'] == 'error'


async def test_resolve_uses_policy_patterns(gateway):
    scoped = gateway._client.options.return_value
    scoped.indices.resolve_index = AsyncMock(return_value=MagicMock(body={}))
    with patch('utils.elasticsearch.get_access_token', return_value=token(['helpdesk'])):
        await gateway.resolve_accessible()
    assert scoped.indices.resolve_index.call_args.kwargs['name'] == ['ecs-microsoft-windows-*']


async def test_esql_requires_mapped_roles(gateway):
    with patch('utils.elasticsearch.get_access_token', return_value=token([])):
        with pytest.raises(ToolError, match='do not grant access'):
            await gateway.esql('FROM x', {})
    gateway._broker.key_for.assert_not_called()


def _shards(total, failed, skipped=0):
    body = {'_shards': {'total': total, 'successful': total - failed, 'skipped': skipped, 'failed': failed},
            'hits': {'hits': [], 'total': {'value': 0}}}
    if failed:
        body['_shards']['failures'] = [{'reason': {
            'type': 'query_shard_exception', 'reason': 'Failed to parse query [x:(]',
            'caused_by': {'type': 'parse_exception', 'reason': 'Encountered EOF'}}}]
    return body


async def test_all_searched_shards_failing_is_an_error(gateway):
    # Seen live: shards skipped by the time filter plus failures on the rest -> HTTP 200, 0 hits.
    gateway._client.options.return_value.search = AsyncMock(return_value=MagicMock(body=_shards(20, 10, skipped=10)))
    with patch('utils.elasticsearch.get_access_token', return_value=token(['helpdesk'])), \
            patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match=r'Failed to parse query \[x:\(\]: Encountered EOF'):
            await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert audit.call_args.kwargs['outcome'] == 'error'


async def test_partial_shard_failure_is_returned_and_audited(gateway):
    gateway._client.options.return_value.search = AsyncMock(return_value=MagicMock(body=_shards(20, 5)))
    with patch('utils.elasticsearch.get_access_token', return_value=token(['helpdesk'])), \
            patch('utils.elasticsearch.audit') as audit:
        body = await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert body['_shards']['failed'] == 5
    assert audit.call_args.kwargs['outcome'] == 'partial' and audit.call_args.kwargs['shards_failed'] == 5
