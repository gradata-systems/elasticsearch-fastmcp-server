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
                                  max_aggregation_range_days=366,
                                  tool_prefix="", cluster_name="", cluster_description="")
    gw.field_caps = AsyncMock(side_effect=lambda index, fields: mapping(*fields))
    return gw


def mapping(*names: str, **types: str) -> dict:
    """A field_caps response with `names` as keyword fields (dates if named like a timestamp) and `types`
    giving other fields' types by name, with '__' for '.'."""
    def caps(kind):
        return {kind: {'type': kind, 'aggregatable': kind not in ('text', 'object')}}
    fields = {n: caps('date' if 'timestamp' in n or n == 'ts' else 'keyword') for n in names if '*' not in n}
    fields |= {name.replace('__', '.'): caps(kind) for name, kind in types.items()}
    return {'indices': ['i'], 'fields': fields}


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
    assert result == {'total': 42, 'returned': 1, 'columns': ['user.name'], 'rows': [['alice']]}
    index, params = es.search.call_args.args[0], es.search.call_args.kwargs
    assert index == 'i' and params['size'] == 5 and params['_source'] == ['user.name'] and 'aggs' not in params
    assert params['sort'] == [{'@timestamp': {'order': 'asc', 'unmapped_type': 'date'}}]
    assert {'term': {'user.name': 'alice'}} in params['query']['bool']['filter']


async def test_top_values(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 100}}, 'aggregations': {'top': {
        'buckets': [{'key': 'alice', 'doc_count': 60}, {'key': 1, 'key_as_string': 'true', 'doc_count': 30}],
        'sum_other_doc_count': 10}}})
    result = await generic.top_values('i', 'user.name', TR, ctx, size=2)
    assert result == {'total_events': 100, 'events_with_other_values': 10, 'columns': ['user.name', 'count'],
                      'rows': [['alice', 60], ['true', 30]]}
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
    assert result['columns'] == ['n', 'c'] and result['rows'] == [['a', 1], ['b', 2]] and result['truncated']
    query, time_filter = es.esql.call_args.args
    assert 'ts' in time_filter['range']


async def test_esql_rejects_double_quoted_field_names(es, ctx):
    es.esql = AsyncMock()
    with pytest.raises(ToolError, match='"Field.Name" is a string, not a field name'):
        await generic.esql_query('FROM i | WHERE "Field.Name" == "Value" | STATS c = COUNT(*)', TR, ctx)
    es.esql.assert_not_called()


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

    assert result['columns'] == ['event.code', 'status', 'before_count', 'after_count', 'before_per_day',
                                 'after_per_day', 'change_percent']
    assert result['rows'] == [['4624', 'stopped', 1000, 0, 100.0, 0.0, -100],
                              ['4688', 'dropped', 200, 20, 20.0, 4.0, -80]]
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
    assert [(r[0], r[1], r[-1]) for r in result['rows']] == [('c', 'increased', 900), ('b', 'new', None)]


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
    assert result['columns'] == ['user.name', 'count', 'first_seen', 'last_seen']
    assert [row[0] for row in result['rows']] == ['bob', 'carol', 'alice']  # most recently seen first
    assert result['rows'][0] == ['bob', 1, '2026-02-01T00:00:00Z', '2026-09-01T00:00:00Z']
    first, second = (call.kwargs for call in es.search.call_args_list)
    assert first['aggs']['groups']['composite']['sources'] == [{'user.name': {'terms': {'field': 'user.name'}}}]
    assert first['aggs']['groups']['aggs']['last_seen'] == {'max': {'field': '@timestamp'}}
    assert second['aggs']['groups']['composite']['after'] == {'user.name': 'bob'}


async def test_distinct_values_orders_and_flags_incomplete_lists(es, ctx, monkeypatch):
    monkeypatch.setattr(generic, '_MAX_GROUPS', 2)
    es.search = AsyncMock(return_value=_distinct_page(
        [('alice', 5, None, None), ('bob', 9, None, None)], after_key={'user.name': 'bob'}))
    result = await generic.distinct_values('i', 'user.name', TR, ctx, order='count', size=1)
    assert [row[0] for row in result['rows']] == ['bob']
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


async def test_larger_searches_summarise_every_matching_event(es, ctx):
    from sources.packs import SourcePack
    ctx.lifespan_context['packs'] = [SourcePack(name='w', title='W', description='d', index='i*',
                                                event_type_field='event.code')]
    es.field_caps = AsyncMock(side_effect=lambda index, fields: mapping(
        '@timestamp', 'event.code', 'user.name', message='text'))
    es.search = AsyncMock(return_value={
        'hits': {'total': {'value': 812}, 'hits': [{'_source': {'user': {'name': 'a'}, 'event': {'code': '1'}}}]},
        'aggregations': {'first': {'value_as_string': '2026-09-01T00:00:00Z'},
                         'last': {'value_as_string': '2026-09-02T00:00:00Z'},
                         'top_0': {'buckets': [{'key': '4624', 'doc_count': 800}]},
                         'top_1': {'buckets': [{'key': 'a', 'doc_count': 500}, {'key': 'b', 'doc_count': 312}]}}})
    result = await generic.search_events('i', TR, ctx, fields=['@timestamp', 'user.name', 'message'], size=20)

    assert es.search.call_args.kwargs['aggs'] == {
        'first': {'min': {'field': '@timestamp'}}, 'last': {'max': {'field': '@timestamp'}},
        'top_0': {'terms': {'field': 'event.code', 'size': 5}}, 'top_1': {'terms': {'field': 'user.name', 'size': 5}}}
    summary = result['summary']
    assert summary['first_event'] == '2026-09-01T00:00:00Z' and summary['last_event'] == '2026-09-02T00:00:00Z'
    assert summary['top_values'] == {'event.code': [['4624', 800]], 'user.name': [['a', 500], ['b', 312]]}
    assert 'all 812 matching events' in summary['note']
    assert result['columns'] == ['@timestamp', 'user.name', 'message'] and result['rows'] == [[None, 'a', None]]


async def test_whole_events_are_flattened(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 1}, 'hits': [
        {'_index': 'i', '_id': '1', '_source': {'user': {'name': 'a', 'roles': ['x']}, 'tags': [], 'o': {}}}]}})
    result = await generic.search_events('i', TR, ctx, fields=['user.*'])
    assert result['events'] == [{'user.name': 'a', 'user.roles': ['x'], 'tags': [], 'o': {}}]


async def test_esql_suggests_stats_for_many_raw_rows(es, ctx):
    es.esql = AsyncMock(return_value={'columns': [{'name': 'n'}], 'values': [[i] for i in range(25)]})
    assert 'STATS' in (await generic.esql_query('FROM i | LIMIT 25', TR, ctx))['hint']
    assert 'hint' not in await generic.esql_query('FROM i | STATS n = COUNT(*) BY x | LIMIT 25', TR, ctx)


async def test_max_result_size_caps_every_tool_and_says_so(es, ctx):
    es.settings.max_result_size = 2
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 100}}, 'aggregations': {'top': {
        'buckets': [{'key': 'a', 'doc_count': 60}, {'key': 'b', 'doc_count': 30}], 'sum_other_doc_count': 10}}})
    result = await generic.top_values('i', 'user.name', TR, ctx, size=50)
    assert es.search.call_args.kwargs['aggs']['top']['terms']['size'] == 2
    assert result['truncated'] and 'at most 2 rows' in result['hint']

    es.search = AsyncMock(return_value=_distinct_page([('a', 1, None, 'x'), ('b', 1, None, 'y'), ('c', 1, None, 'z')]))
    result = await generic.distinct_values('i', 'user.name', TR, ctx)
    assert result['returned'] == 2 and result['distinct_values'] == 3 and 'at most 2 rows' in result['hint']

    es.search = AsyncMock(return_value={'hits': {'total': {'value': 9}, 'hits': [{'_source': {}}] * 2}})
    result = await generic.search_events('i', TR, ctx, size=10)
    assert es.search.call_args.kwargs['size'] == 2 and 'at most 2 rows' in result['hint']


async def test_search_within_the_cap_is_not_marked_truncated(es, ctx):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 9}, 'hits': [{'_source': {}}] * 5}})
    assert 'truncated' not in await generic.search_events('i', TR, ctx, size=5)


async def test_top_values_keeps_to_the_response_budget(es, ctx):
    es.settings.max_response_chars = 30
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 3}}, 'aggregations': {'top': {
        'buckets': [{'key': 'a' * 20, 'doc_count': 2}, {'key': 'b' * 20, 'doc_count': 1}]}}})
    result = await generic.top_values('i', 'user.name', TR, ctx)
    assert result['rows'] == [['a' * 20, 2]] and result['truncated']


async def test_list_data_sources_lists_only_as_many_indices_as_fit(es, ctx):
    from sources.packs import SourcePack
    es.settings.max_response_chars = 1000
    ctx.lifespan_context['packs'] = [SourcePack(name='l', title='L', description='d', index='logs-2026.09.30')]
    es.resolve_accessible = AsyncMock(return_value={
        'indices': [{'name': f'logs-2026.{m:02}.{d:02}'} for m in range(1, 10) for d in range(1, 31)],
        'aliases': [], 'data_streams': []})
    es.resolve_accessible.return_value['indices'].append({'name': 'logs-2026.09.30'})
    result = await generic.list_data_sources(ctx)
    assert 0 < len(result['indices']) < 50 and result['truncated'] and "'index' pattern" in result['hint']
    assert [s['name'] for s in result['sources']] == ['l']  # found among all indices, not just those listed


async def test_searches_that_timed_out_are_marked_partial(es, ctx):
    es.search = AsyncMock(return_value={'timed_out': True, 'hits': {'total': {'value': 1}, 'hits': [{'_source': {}}]}})
    result = await generic.search_events('i', TR, ctx)
    assert 'incomplete' in result['warning'] and 'time limit' in result['warning']


def _values_page(field, buckets, after_key=None):
    """A composite page of `field` values, with first_seen and last_seen when buckets give them."""
    agg = {'buckets': []}
    for value, count, *seen in buckets:
        bucket = {'key': {field: value}, 'doc_count': count}
        if seen:
            bucket |= {'first_seen': {'value_as_string': seen[0]}, 'last_seen': {'value_as_string': seen[1]}}
        agg['buckets'].append(bucket)
    if after_key:
        agg['after_key'] = after_key
    return {'aggregations': {'groups': agg}}


async def test_match_values_looks_up_the_first_sources_values_in_the_second(es, ctx):
    es.search = AsyncMock(side_effect=[
        _values_page('user.name', [('alice', 3), ('bob', 1), ('carol', 7)]),
        _values_page('winlog.user', [('alice', 5, '2026-09-01T01:00:00Z', '2026-09-01T09:00:00Z'),
                                     ('carol', 2, '2026-09-01T02:00:00Z', '2026-09-02T03:00:00Z')]),
    ])
    result = await generic.match_values(
        'vpn-*', 'user.name', TR, 'win-*', 'winlog.user', ctx,
        filters=[Filter(field='group', value='admins')],
        match_filters=[Filter(field='event.code', value='4624')])
    assert result == {
        'values_checked': 3, 'matched': 2, 'unmatched': 1, 'returned': 2,
        'columns': ['user.name', 'match_count', 'first_seen', 'last_seen'],
        'rows': [['carol', 2, '2026-09-01T02:00:00Z', '2026-09-02T03:00:00Z'],
                 ['alice', 5, '2026-09-01T01:00:00Z', '2026-09-01T09:00:00Z']]}
    (source_index,), source = es.search.call_args_list[0]
    (match_index,), lookup = es.search.call_args_list[1]
    assert source_index == 'vpn-*' and {'term': {'group': 'admins'}} in source['query']['bool']['filter']
    assert match_index == 'win-*'
    assert {'terms': {'winlog.user': ['alice', 'bob', 'carol']}} in lookup['query']['bool']['filter']
    assert {'term': {'event.code': '4624'}} in lookup['query']['bool']['filter']
    assert lookup['aggs']['groups']['aggs']['last_seen'] == {'max': {'field': '@timestamp'}}


async def test_match_values_shows_unmatched_values_and_matches_numbers_to_strings(es, ctx):
    es.search = AsyncMock(side_effect=[
        _values_page('id', [('7', 1), ('8', 1)]),
        _values_page('uid', [(7, 4, None, None)]),
    ])
    result = await generic.match_values('a', 'id', TR, 'b', 'uid', ctx, show='both', order='value')
    assert result['rows'] == [['7', 4, None, None], ['8', 0, None, None]]
    es.search = AsyncMock(side_effect=[_values_page('id', [('7', 1), ('8', 1)]), _values_page('uid', [])])
    result = await generic.match_values('a', 'id', TR, 'b', 'uid', ctx, show='unmatched')
    assert [row[0] for row in result['rows']] == ['7', '8'] and 'same kind of value' in result['hint']


async def test_match_values_looks_up_values_a_page_at_a_time(es, ctx, monkeypatch):
    monkeypatch.setattr(generic, '_PAGE_SIZE', 2)
    es.search = AsyncMock(side_effect=[
        _values_page('u', [('a', 1), ('b', 1)], after_key={'u': 'b'}),
        _values_page('u', [('c', 1)]),
        _values_page('v', [('a', 1, None, '2026-09-01T00:00:00Z')]),
        _values_page('v', [('c', 1, None, '2026-09-02T00:00:00Z')]),
    ])
    result = await generic.match_values('a', 'u', TR, 'b', 'v', ctx,
                                        match_time_range=TimeRange(start='2026-08-01', end='2026-09-02'))
    assert [row[0] for row in result['rows']] == ['c', 'a']
    lookups = [call.kwargs['query']['bool']['filter'] for call in es.search.call_args_list[2:]]
    assert [f[1] for f in lookups] == [{'terms': {'v': ['a', 'b']}}, {'terms': {'v': ['c']}}]
    assert lookups[0][0]['range']['@timestamp']['gte'].startswith('2026-08-01')


async def test_match_values_flags_an_incomplete_set_of_values(es, ctx, monkeypatch):
    monkeypatch.setattr(generic, '_MAX_GROUPS', 1)
    es.search = AsyncMock(side_effect=[
        _values_page('u', [('a', 1)], after_key={'u': 'a'}),
        _values_page('v', [('a', 1, None, None)]),
    ])
    result = await generic.match_values('a', 'u', TR, 'b', 'v', ctx)
    assert result['values_complete'] is False and 'more than 1 distinct' in result['hint']
