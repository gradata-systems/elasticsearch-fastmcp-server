from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from elasticsearch import AuthenticationException, AuthorizationException, BadRequestError
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken

from config import Settings
from security.policy import AccessPolicy
from utils.elasticsearch import ElasticsearchGateway

SETTINGS = Settings(
    _env_file=None,
    es_url='https://es.example:9200',
    es_impersonator_username='mcp_impersonator',
    es_impersonator_password='secret',
    oidc_issuer='https://idp.example/realms/r',
    oidc_audience='es-mcp',
    public_base_url='https://mcp.example',
    max_result_size=100,
)
POLICY = AccessPolicy(exposed_indices=['ecs-microsoft-windows-*'], impersonable_users=['*.*', 'service-account-*'])
RUN_AS = ElasticsearchGateway.RUN_AS_HEADER


def token(username='john.smith1'):
    claims = {'preferred_username': username} if username is not None else {}
    return AccessToken(token='t', client_id='c', scopes=[], subject='sub-1', claims=claims)


def as_user(username='john.smith1'):
    return patch('utils.elasticsearch.get_access_token', return_value=token(username))


def es_error(cls, status, message):
    return cls(message, MagicMock(status=status), {'error': {'type': 'security_exception', 'reason': message}})


@pytest.fixture
def gateway():
    gw = ElasticsearchGateway(SETTINGS, POLICY)
    scoped = MagicMock()
    scoped.search = AsyncMock(return_value=MagicMock(body={'hits': {'hits': []}}))
    gw._client = MagicMock()
    gw._client.options.return_value = scoped
    return gw


async def test_unauthenticated_is_rejected(gateway):
    with patch('utils.elasticsearch.get_access_token', return_value=None):
        with pytest.raises(ToolError, match='Not authenticated'):
            await gateway.search('ecs-microsoft-windows-v1', size=10)
    gateway._client.options.assert_not_called()


@pytest.mark.parametrize('username', [None, 'elastic', 'svc_avw_api', 'mcp_impersonator', 'a.b,elastic'])
async def test_non_impersonable_username_never_reaches_es(gateway, username):
    with as_user(username), patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match='not permitted'):
            await gateway.search('ecs-microsoft-windows-v1', size=10)
    gateway._client.options.assert_not_called()
    audit.assert_called_once_with('access_denied', reason='username_not_impersonable', es_user=username)


async def test_index_outside_exposed_patterns_is_rejected_before_es(gateway):
    with as_user(), patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match='not available through this server'):
            await gateway.search('ecs-ingress-nginx-access-v1', size=10)
    gateway._client.options.assert_not_called()
    audit.assert_called_once_with('access_denied', reason='index_not_exposed', api='search',
                                  es_user='john.smith1', index='ecs-ingress-nginx-access-v1')


async def test_search_runs_as_caller_and_caps_size(gateway):
    with as_user(), patch('utils.elasticsearch.audit') as audit:
        await gateway.search('ecs-microsoft-windows-v1', size=10_000, query={'match_all': {}})
    event, fields = audit.call_args.args[0], audit.call_args.kwargs
    assert event == 'es_request' and fields['outcome'] == 'success' and fields['es_user'] == 'john.smith1'
    # Elasticsearch stops just before the client's 30 second request timeout.
    assert fields['request'] == {'size': 100, 'query': {'match_all': {}}, 'timeout': '27000ms'}

    gateway._client.options.assert_called_once_with(headers={RUN_AS: 'john.smith1'}, opaque_id='mcp:john.smith1')
    gateway._client.options.return_value.search.assert_awaited_once_with(
        index='ecs-microsoft-windows-v1', size=100, query={'match_all': {}}, timeout='27000ms')


async def test_agent_service_account_runs_as_itself(gateway):
    with as_user('service-account-langgraph-triage'):
        await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert gateway._client.options.call_args.kwargs['headers'] == {RUN_AS: 'service-account-langgraph-triage'}


@pytest.mark.parametrize('error, reason, message', [
    (es_error(AuthorizationException, 403,
              'action [indices:data/read/search] is unauthorized for user [mcp_impersonator] '
              'run as [john.smith1] with effective roles [] on indices [ecs-microsoft-windows-v1]'),
     'elasticsearch_403', 'denied access'),
    (es_error(AuthorizationException, 403,
              'action [x] is unauthorized for user [mcp_impersonator], because user [mcp_impersonator] '
              'is unauthorized to run as [john.smith1]'),
     'run_as_denied', "no usable account 'john.smith1'"),
    (es_error(AuthenticationException, 401, 'unable to authenticate user [john.smith1]'),
     'run_as_denied', "no usable account 'john.smith1'"),
])
async def test_es_security_errors(gateway, error, reason, message):
    gateway._client.options.return_value.search = AsyncMock(side_effect=error)
    with as_user(), patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match=message):
            await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert audit.call_args.args[0] == 'access_denied' and audit.call_args.kwargs['reason'] == reason


async def test_es_error_reason_is_surfaced(gateway):
    error = BadRequestError('bad', MagicMock(status=400), {'error': {
        'root_cause': [{'reason': 'Failed to parse query [a:(]'}], 'caused_by': {'reason': 'Encountered EOF'}}})
    gateway._client.options.return_value.search = AsyncMock(side_effect=error)
    with as_user(), patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match=r'Failed to parse query \[a:\(\]: Encountered EOF'):
            await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert audit.call_args.kwargs['outcome'] == 'error'


async def test_resolve_uses_exposed_patterns_as_caller(gateway):
    scoped = gateway._client.options.return_value
    scoped.indices.resolve_index = AsyncMock(return_value=MagicMock(body={}))
    with as_user():
        await gateway.resolve_accessible()
    assert scoped.indices.resolve_index.call_args.kwargs['name'] == ['ecs-microsoft-windows-*']
    assert gateway._client.options.call_args.kwargs['headers'] == {RUN_AS: 'john.smith1'}


async def test_esql_sources_are_checked_against_exposed_patterns(gateway):
    scoped = gateway._client.options.return_value
    scoped.esql.query = AsyncMock(return_value=MagicMock(body={'columns': [], 'values': []}))
    with as_user():
        await gateway.esql('FROM ecs-microsoft-windows-v1 | LIMIT 1', {})
        with pytest.raises(ToolError, match="'ecs-microsoft-windows-v1,avw_contacts' is not available"):
            await gateway.esql('FROM ecs-microsoft-windows-v1 | LOOKUP JOIN avw_contacts ON user.name', {})
        with pytest.raises(ToolError, match='must start with FROM'):
            await gateway.esql('ROW a = 1', {})
    scoped.esql.query.assert_awaited_once()


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
    with as_user(), patch('utils.elasticsearch.audit') as audit:
        with pytest.raises(ToolError, match=r'Failed to parse query \[x:\(\]: Encountered EOF'):
            await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert audit.call_args.kwargs['outcome'] == 'error'


async def test_partial_shard_failure_is_returned_and_audited(gateway):
    gateway._client.options.return_value.search = AsyncMock(return_value=MagicMock(body=_shards(20, 5)))
    with as_user(), patch('utils.elasticsearch.audit') as audit:
        body = await gateway.search('ecs-microsoft-windows-v1', size=1)
    assert body['_shards']['failed'] == 5
    assert audit.call_args.kwargs['outcome'] == 'partial' and audit.call_args.kwargs['shards_failed'] == 5
