from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from tools import generic
from tools.query import Filter, TimeRange

TR = TimeRange(start='2026-09-01', end='2026-09-02')


@pytest.fixture
def es():
    gw = MagicMock()
    gw.settings = SimpleNamespace(max_result_size=500, max_response_chars=100_000, max_time_range_days=90,
                                  max_aggregation_range_days=366)
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


async def test_list_data_sources_matches_multi_target_pack_indices(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [
        SourcePack(name='multi', title='M', description='d', index='x-*,-x-debug-*,logs'),
        SourcePack(name='excluded', title='E', description='d', index='a-*,-a-*')]
    es.resolve_accessible = AsyncMock(return_value={
        'indices': [{'name': 'a-1'}, {'name': 'x-debug-1'}], 'aliases': [], 'data_streams': [{'name': 'logs'}]})
    assert [s['name'] for s in (await generic.list_data_sources(ctx))['sources']] == ['multi']


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


def _groups_page(buckets, after_key=None):
    agg = {'buckets': [{'key': key, 'before': {'doc_count': b}, 'after': {'doc_count': a}}
                       for key, b, a in buckets]}
    if after_key:
        agg['after_key'] = after_key
    return {'aggregations': {'groups': agg}}


BEFORE = TimeRange(start='2026-08-01', end='2026-08-10')  # 10 days
AFTER = TimeRange(start='2026-08-11', end='2026-08-15')  # 5 days


async def test_compare_periods_pages_through_groups_and_ranks_drops(es, ctx):
    es.search = AsyncMock(side_effect=[
        _groups_page([({'event.code': '4624'}, 1000, 0), ({'event.code': '4625'}, 100, 50)],
                     after_key={'event.code': '4625'}),
        _groups_page([({'event.code': '4688'}, 200, 20), ({'event.code': '4634'}, 3, 0),
                      ({'event.code': '4672'}, 0, 40)]),
    ])
    result = await generic.compare_periods('i', BEFORE, AFTER, ctx, group_by=['event.code'])

    assert [(c['group']['event.code'], c['status']) for c in result['changes']] == [
        ('4624', 'stopped'), ('4688', 'dropped')]
    assert result['changes'][1] == {
        'group': {'event.code': '4688'}, 'status': 'dropped', 'before_count': 200, 'after_count': 20,
        'before_per_day': 20.0, 'after_per_day': 4.0, 'change_percent': -80}
    assert result['groups_compared'] == 5 and result['groups_changed'] == 2
    assert result['before_days'] == 10.0 and result['after_days'] == 5.0

    first, second = (call.kwargs for call in es.search.call_args_list)
    assert 'after' not in first['aggs']['groups']['composite']
    assert second['aggs']['groups']['composite']['after'] == {'event.code': '4625'}
    assert first['aggs']['groups']['composite']['sources'] == [{'event.code': {'terms': {'field': 'event.code'}}}]
    periods = first['query']['bool']['filter'][0]['bool']['should']
    assert periods[0] == first['aggs']['groups']['aggs']['before']['filter']
    assert periods[1]['range']['@timestamp']['gte'].startswith('2026-08-11')


async def test_compare_periods_reports_increases_when_asked(es, ctx):
    es.search = AsyncMock(return_value=_groups_page([
        ({'h': 'a'}, 100, 0), ({'h': 'b'}, 0, 40), ({'h': 'c'}, 10, 50), ({'h': 'd'}, 10, 5)]))
    result = await generic.compare_periods('i', BEFORE, AFTER, ctx, ['h'], changes='up')
    assert [(c['group']['h'], c['status'], c['change_percent']) for c in result['changes']] == [
        ('c', 'increased', 900), ('b', 'new', None)]


async def test_compare_periods_checks_each_period_separately(es, ctx):
    es.search = AsyncMock(return_value=_groups_page([]))
    # Over a year apart, and longer than the event search limit, but each within the aggregation limit.
    await generic.compare_periods('i', TimeRange(start='2025-01-01', end='2025-06-30'), AFTER, ctx, ['h'])
    with pytest.raises(ToolError, match='maximum of 366 days'):
        await generic.compare_periods('i', TimeRange(start='2024-01-01', end='2025-06-30'), AFTER, ctx, ['h'])
    with pytest.raises(ToolError, match='must end before'):
        await generic.compare_periods('i', BEFORE, TimeRange(start='2026-08-05'), ctx, ['h'])


async def test_compare_periods_notes_empty_baseline_and_group_cap(es, ctx, monkeypatch):
    monkeypatch.setattr(generic, '_MAX_GROUPS', 2)
    es.search = AsyncMock(return_value=_groups_page([({'h': 'a'}, 0, 10), ({'h': 'b'}, 0, 10)], after_key={'h': 'b'}))
    result = await generic.compare_periods('i', BEFORE, AFTER, ctx, ['h'])
    assert es.search.await_count == 1
    assert 'first 2 groups' in result['note'] and 'not be kept that far back' in result['note']


def _days_ago(days):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days)).isoformat()


async def test_search_notes_retention_of_covered_packs(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [
        SourcePack(name='fw', title='Firewall', description='d', index='fw-*', retention_days=30),
        SourcePack(name='win', title='Windows', description='d', index='win-*', retention_days=60)]
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 0}, 'hits': []}})

    result = await generic.search_events('win-*,fw-*', TimeRange(start=_days_ago(45)), ctx)
    assert result['note'].startswith('Firewall keeps every event for only 30 days')
    result = await generic.search_events('win-*', TimeRange(start=_days_ago(45)), ctx)
    assert 'note' not in result


async def test_compare_periods_warns_when_baseline_predates_retention(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [SourcePack(name='w', title='Windows', description='d', index='win-*',
                                                retention_days=60)]
    es.search = AsyncMock(return_value=_groups_page([({'h': 'a'}, 10, 10)]))
    result = await generic.compare_periods(
        'win-*', TimeRange(start=_days_ago(80), end=_days_ago(31)), TimeRange(start=_days_ago(30)), ctx, ['h'])
    assert 'only 60 days' in result['note'] and 'baseline is incomplete' in result['note']


def _distinct_page(buckets, after_key=None):
    agg = {'buckets': [{'key': {'user.name': name}, 'doc_count': count,
                        'first_seen': {'value': 1, 'value_as_string': first},
                        'last_seen': {'value': 2, 'value_as_string': last}} for name, count, first, last in buckets]}
    if after_key:
        agg['after_key'] = after_key
    return {'aggregations': {'groups': agg}}


async def test_distinct_values_lists_every_value_across_pages(es, ctx):
    es.search = AsyncMock(side_effect=[
        _distinct_page([('alice', 5, '2026-01-02T00:00:00Z', '2026-03-01T00:00:00Z'),
                        ('bob', 1, '2026-02-01T00:00:00Z', '2026-09-01T00:00:00Z')], after_key={'user.name': 'bob'}),
        _distinct_page([('carol', 9, '2026-01-01T00:00:00Z', '2026-05-01T00:00:00Z')]),
    ])
    # A year: past the event search limit but within the aggregation limit.
    result = await generic.distinct_values('i', 'user.name', TimeRange(start='2025-10-01', end='2026-09-28'), ctx)
    assert result['distinct_values'] == 3
    assert [v['value'] for v in result['values']] == ['bob', 'carol', 'alice']  # most recently seen first
    assert result['values'][0] == {'value': 'bob', 'count': 1, 'first_seen': '2026-02-01T00:00:00Z',
                                   'last_seen': '2026-09-01T00:00:00Z'}
    first, second = (call.kwargs for call in es.search.call_args_list)
    assert first['aggs']['groups']['composite']['sources'] == [{'user.name': {'terms': {'field': 'user.name'}}}]
    assert first['aggs']['groups']['aggs']['last_seen'] == {'max': {'field': '@timestamp'}}
    assert second['aggs']['groups']['composite']['after'] == {'user.name': 'bob'}


async def test_distinct_values_orders_and_flags_incomplete_lists(es, ctx, monkeypatch):
    monkeypatch.setattr(generic, '_MAX_GROUPS', 2)
    es.search = AsyncMock(return_value=_distinct_page(
        [('alice', 5, None, None), ('bob', 9, None, None)], after_key={'user.name': 'bob'}))
    result = await generic.distinct_values('i', 'user.name', TR, ctx, order='count', size=1)
    assert [v['value'] for v in result['values']] == ['bob']
    assert result['distinct_values_complete'] is False and 'more than 2' in result['hint']


async def test_compare_periods_defaults_to_the_sources_event_type_field(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [
        SourcePack(name='w', title='W', description='d', index='win-*', event_type_field='event.code'),
        SourcePack(name='f', title='F', description='d', index='fw-*', event_type_field='event.type_id'),
        SourcePack(name='n', title='N', description='d', index='web-*')]
    es.search = AsyncMock(return_value=_groups_page([]))

    result = await generic.compare_periods('win-*', BEFORE, AFTER, ctx)
    assert result['group_by'] == ['event.code']
    assert es.search.call_args.kwargs['aggs']['groups']['composite']['sources'] == [
        {'event.code': {'terms': {'field': 'event.code'}}}]
    with pytest.raises(ToolError, match='different fields'):
        await generic.compare_periods('win-*,fw-*', BEFORE, AFTER, ctx)
    with pytest.raises(ToolError, match='pass group_by'):
        await generic.compare_periods('web-*', BEFORE, AFTER, ctx)


async def test_esql_with_stats_allows_the_aggregation_range_and_notes_retention(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [SourcePack(name='w', title='Windows', description='d', index='win-*',
                                                retention_days=60)]
    es.esql = AsyncMock(return_value={'columns': [{'name': 'n'}], 'values': [[1]]})
    year = TimeRange(start=_days_ago(300))

    result = await generic.esql_query('FROM win-* | STATS n = COUNT(*)', year, ctx)
    assert 'only 60 days' in result['note']
    with pytest.raises(ToolError, match='maximum of 90 days'):
        await generic.esql_query('FROM win-* | WHERE message == "| STATS" | LIMIT 5', year, ctx)
    with pytest.raises(ToolError, match='maximum of 90 days'):
        await generic.esql_query('FROM win-* | INLINE STATS n = COUNT(*) | LIMIT 5', year, ctx)
