from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import generic
from tools.query import Filter, TimeRange

TR = TimeRange(start='2026-09-01', end='2026-09-02')


@pytest.fixture
def es():
    gw = MagicMock()
    gw.settings = SimpleNamespace(max_result_size=500, max_response_chars=100_000, max_time_range_days=90)
    return gw


@pytest.fixture
def ctx(es):
    return SimpleNamespace(lifespan_context={'es': es})


async def test_list_data_sources_excludes_backing_indices_and_describes_readable_packs(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [
        SourcePack(name='logs', title='Logs', description='App  logs', index='log*', key_fields={'f': 'meaning'}),
        SourcePack(name='hidden', title='H', description='d', index='not-readable')]
    es.resolve_accessible = AsyncMock(return_value={
        'indices': [{'name': 'b-1'}, {'name': 'a-1'}, {'name': '.ds-logs-1', 'data_stream': 'logs'}],
        'aliases': [{'name': 'a'}], 'data_streams': [{'name': 'logs'}]})
    assert await generic.list_data_sources(ctx) == {
        'indices': ['a-1', 'b-1'], 'aliases': ['a'], 'data_streams': ['logs'],
        'sources': [{'name': 'logs', 'title': 'Logs', 'description': 'App logs', 'index': 'log*',
                     'key_fields': {'f': 'meaning'}, 'tools': []}]}


async def test_describe_fields_flattens_types(es, ctx):
    es.field_caps = AsyncMock(return_value={'indices': ['i'], 'fields': {
        '_id': {'_id': {}}, 'user': {'object': {}}, 'user.name': {'keyword': {}},
        'source.ip': {'ip': {}, 'keyword': {}}}})
    assert await generic.describe_fields('i', ctx, 'user.*, source.ip') == {
        'indices': ['i'], 'fields': {'source.ip': ['ip', 'keyword'], 'user.name': 'keyword'}}
    # A comma-joined string is treated by ES as one literal pattern, so it must be split.
    es.field_caps.assert_awaited_once_with('i', ['user.*', 'source.ip'])


async def test_describe_fields_truncates(es, ctx):
    es.settings.max_response_chars = 30
    es.field_caps = AsyncMock(return_value={'indices': [], 'fields': {f'f{i}': {'keyword': {}} for i in range(10)}})
    result = await generic.describe_fields('i', ctx)
    assert result['truncated'] and len(result['fields']) < 10


async def test_search_events_builds_request_and_shapes_hits(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 42}, 'hits': [
        {'_index': 'i', '_id': '1', '_source': {'user': {'name': 'alice'}}}]}})
    result = await generic.search_events('i', TR, ctx, filters=[Filter(field='user.name', value='alice')],
                                         fields=['user.name'], size=5, sort='asc')
    assert result == {'total': 42, 'returned': 1,
                      'events': [{'index': 'i', 'id': '1', 'event': {'user': {'name': 'alice'}}}]}
    index, params = es.search.call_args.args[0], es.search.call_args.kwargs
    assert index == 'i' and params['size'] == 5 and params['_source'] == ['user.name']
    assert params['sort'] == [{'@timestamp': {'order': 'asc', 'unmapped_type': 'date'}}]
    assert {'term': {'user.name': 'alice'}} in params['query']['bool']['filter']


async def test_top_values(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 100}}, 'aggregations': {'top': {
        'buckets': [{'key': 'alice', 'doc_count': 60}, {'key': 1, 'key_as_string': 'true', 'doc_count': 30}],
        'sum_other_doc_count': 10}}})
    result = await generic.top_values('i', 'user.name', TR, ctx, size=2)
    assert result == {'total_events': 100, 'events_with_other_values': 10,
                      'values': [{'value': 'alice', 'count': 60}, {'value': 'true', 'count': 30}]}
    assert es.search.call_args.kwargs['aggs'] == {'top': {'terms': {'field': 'user.name', 'size': 2}}}


async def test_top_values_hints_when_field_absent(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 5}}, 'aggregations': {'top': {'buckets': []}}})
    result = await generic.top_values('i', 'event.action', TR, ctx)
    assert 'describe_fields' in result['hint']


async def test_search_events_warns_on_partial_shard_failure(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 0}, 'hits': []}, '_shards': {
        'total': 4, 'failed': 1, 'failures': [{'reason': {'reason': 'boom'}}]}})
    result = await generic.search_events('i', TR, ctx)
    assert result['warning'].endswith('boom')


async def test_esql_applies_time_filter_and_caps_rows(es, ctx):
    es.settings.max_result_size = 2
    es.esql = AsyncMock(return_value={'columns': [{'name': 'n'}, {'name': 'c'}],
                                      'values': [['a', 1], ['b', 2], ['c', 3]]})
    result = await generic.esql_query('FROM i | STATS c = COUNT(*) BY n', TR, ctx, timestamp_field='ts')
    assert result['rows'] == [{'n': 'a', 'c': 1}, {'n': 'b', 'c': 2}] and result['truncated']
    query, time_filter = es.esql.call_args.args
    assert 'ts' in time_filter['range']


async def test_tool_call_over_mcp_parses_json_arguments(es):
    from contextlib import asynccontextmanager
    from fastmcp import Client, FastMCP

    @asynccontextmanager
    async def lifespan(server):
        yield {'es': es}

    mcp = FastMCP('t', lifespan=lifespan)
    mcp.tool(generic.search_events)
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 0}, 'hits': []}})
    async with Client(mcp) as client:
        await client.call_tool('search_events', {
            'index': 'i',
            'time_range': {'start': '2026-09-01', 'end': '2026-09-01'},
            'filters': [{'field': 'event.code', 'op': 'in', 'value': [4624, '4625']},
                        {'field': 'user.name', 'value': 'alice', 'negate': True}],
        })
    query = es.search.call_args.kwargs['query']['bool']
    assert query['filter'][0]['range']['@timestamp']['lte'] == '2026-09-01T23:59:59.999+00:00'
    assert query['filter'][1] == {'terms': {'event.code': [4624, '4625']}}
    assert query['must_not'] == [{'term': {'user.name': 'alice'}}]
