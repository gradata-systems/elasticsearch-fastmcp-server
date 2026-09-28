from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from security.policy import Caller
from sources.packs import ParamSpec, SourcePack, SourceTool, ToolSpec, exposed_packs, load_packs

REPO_PACKS = Path(__file__).parent.parent / 'packs'
DAY = {'start': '2026-09-01', 'end': '2026-09-01'}


def test_repo_packs_load_and_validate():
    packs = load_packs(REPO_PACKS)
    assert {p.name for p in packs} == {'windows', 'ingress_nginx', 'fortios'}
    for pack in packs:
        assert pack.tools and pack.key_fields
        for spec in pack.tools:
            SourceTool.build(pack, spec)  # every spec produces a valid tool


def test_param_spec_queries():
    assert ParamSpec(description='d', fields=['user.name']).to_query('a') == {'term': {'user.name': 'a'}}
    assert ParamSpec(description='d', fields=['h'], match='prefix').to_query('x') == {'prefix': {'h': 'x'}}
    assert ParamSpec(description='d', fields=['user.name', 'user.id']).to_query('a') == {'bool': {
        'should': [{'term': {'user.name': 'a'}}, {'term': {'user.id': 'a'}}], 'minimum_should_match': 1}}


@pytest.mark.parametrize('spec', [
    {'name': 'Bad-Name', 'kind': 'search', 'description': 'd'},
    {'name': 't', 'kind': 'top_values', 'description': 'd'},
    {'name': 't', 'kind': 'top_values', 'description': 'd', 'field': 'f', 'size': 500},
    {'name': 't', 'kind': 'search', 'description': 'd', 'params': {'time_range': {'description': 'd', 'fields': ['f']}}},
    {'name': 't', 'kind': 'search', 'description': 'd', 'unknown_key': 1},
    {'name': 't', 'kind': 'search', 'description': 'd',
     'params': {'p': {'description': 'd', 'fields': ['f'], 'type': 'integer', 'match': 'prefix'}}},
])
def test_invalid_tool_specs_rejected(spec):
    with pytest.raises(ValidationError):
        ToolSpec.model_validate(spec)


def test_duplicate_tool_names_across_packs_rejected(tmp_path):
    for name in ('a', 'b'):
        (tmp_path / f'{name}.yaml').write_text(
            f"name: {name}\ntitle: t\ndescription: d\nindex: i\ntools:\n"
            f"  - {{name: same_tool, kind: search, description: d}}\n")
    with pytest.raises(ValueError, match="'same_tool' is defined by both"):
        load_packs(tmp_path)


def test_packs_outside_exposed_indices_are_skipped():
    packs = [SourcePack(name='a', title='t', description='d', index='ecs-a'),
             SourcePack(name='b', title='t', description='d', index='other')]
    assert [p.name for p in exposed_packs(packs, lambda index: index.startswith('ecs-'))] == ['a']


@pytest.mark.parametrize('index', ['a', 'a-*', 'a-*,b', 'a-*,-a-debug-*,b', 'a-*,b,-b-*'])
def test_pack_index_accepts_multi_target_expressions(index):
    assert SourcePack(name='p', title='t', description='d', index=index).index == index


@pytest.mark.parametrize('index', ['', 'a,', 'a, b', '-a-*', 'a,-a-debug', 'a-*,-', 'remote:a', '<a-{now/d}>',
                                   'a/b', 'a|b'])
def test_pack_index_rejects_invalid_expressions(index):
    with pytest.raises(ValidationError):
        SourcePack(name='p', title='t', description='d', index=index)


def test_multi_target_packs_are_exposed_only_if_every_target_is():
    caller = Caller('', '', frozenset({'ecs-a-*', 'ecs-b-*'}))
    packs = [SourcePack(name='both', title='t', description='d', index='ecs-a-*,-ecs-a-rs-*,ecs-b-*'),
             SourcePack(name='partial', title='t', description='d', index='ecs-a-*,ecs-c-*')]
    assert [p.name for p in exposed_packs(packs, caller.may_read)] == ['both']


def _server(es, packs):
    @asynccontextmanager
    async def lifespan(server):
        yield {'es': es, 'packs': packs}

    mcp = FastMCP('t', lifespan=lifespan)
    for pack in packs:
        for spec in pack.tools:
            mcp.add_tool(SourceTool.build(pack, spec))
    return mcp


@pytest.fixture
def es():
    gw = MagicMock()
    gw.settings = SimpleNamespace(max_result_size=500, max_response_chars=100_000, max_time_range_days=90,
                                  max_aggregation_range_days=366,
                                  tool_prefix="", cluster_name="", cluster_description="")
    gw.search = AsyncMock(return_value={'hits': {'total': {'value': 1}, 'hits': [
        {'_index': 'i', '_id': '1', '_source': {'user.name': 'john.smith1'}}]}})
    return gw


@pytest.fixture
def windows():
    return next(p for p in load_packs(REPO_PACKS) if p.name == 'windows')


async def test_search_tool_builds_query_from_params_and_fixed_filters(es, windows):
    async with Client(_server(es, [windows])) as c:
        result = await c.call_tool('windows_remote_access_events', {'user': 'john.smith1', 'time_range': DAY})
    assert result.structured_content['events'][0]['event'] == {'user.name': 'john.smith1'}

    index, params = es.search.call_args.args[0], es.search.call_args.kwargs
    assert index == 'ecs-microsoft-windows-*' and params['size'] == 50 and params['_source'] == ['*']
    assert params['sort'] == [{'@timestamp': {'order': 'asc', 'unmapped_type': 'date'}}]
    clauses = params['query']['bool']['filter']
    assert {'terms': {'event.provider': ['Microsoft-Windows-TerminalServices-Gateway',
                                         'Microsoft-Windows-TerminalServices-RemoteConnectionManager']}} in clauses
    assert {'bool': {'should': [{'term': {'user.name': 'john.smith1'}}, {'term': {'user.id': 'john.smith1'}}],
                     'minimum_should_match': 1}} in clauses


async def test_optional_params_omitted_and_default_fields_used(es, windows):
    async with Client(_server(es, [windows])) as c:
        await c.call_tool('windows_process_executions', {'host': 'habpw01', 'time_range': DAY, 'size': 5})
    params = es.search.call_args.kwargs
    clauses = params['query']['bool']['filter']
    assert {'prefix': {'host.hostname': 'habpw01'}} in clauses and {'term': {'event.code': '4688'}} in clauses
    assert not any('user.name' in str(c) for c in clauses)
    assert params['size'] == 5 and params['_source'] == windows.default_fields


async def test_top_values_tool_with_sample_fields(es, windows):
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 10}}, 'aggregations': {'top': {
        'buckets': [{'key': 'john.smith1', 'doc_count': 10, 'sample': {'hits': {'hits': [
            {'_source': {'user': {'id': 'S-1-5-21-1', 'domain': 'intranet'}}}]}}}],
        'sum_other_doc_count': 0}}})
    async with Client(_server(es, [windows])) as c:
        result = await c.call_tool('windows_active_users', {'time_range': DAY})
    assert result.structured_content['values'] == [
        {'value': 'john.smith1', 'count': 10, 'sample': {'user.id': 'S-1-5-21-1', 'user.domain': 'intranet'}}]
    aggs = es.search.call_args.kwargs['aggs']['top']
    assert aggs['terms'] == {'field': 'user.name', 'size': 50}
    assert aggs['aggs']['sample']['top_hits']['_source'] == ['user.id', 'user.domain']


@pytest.mark.parametrize('args', [
    {'time_range': DAY},                                             # missing required user
    {'user': 'a', 'time_range': DAY, 'size': 501},                   # over the cap
    {'user': 'a', 'time_range': DAY, 'index': 'ww2_nomroll'},        # can't redirect the index
])
async def test_invalid_arguments_rejected(es, windows, args):
    async with Client(_server(es, [windows])) as c:
        with pytest.raises(ToolError):
            await c.call_tool('windows_user_events', args)
    es.search.assert_not_called()


@pytest.mark.parametrize('index, expected', [
    ('ecs-microsoft-windows-*', True),           # the pack's own index
    ('ecs-microsoft-windows-rs-*', True),        # one of its tiers
    ('ecs-microsoft-windows-v1', True),
    ('ecs-*', True),                             # a pattern that covers it
    ('ecs-fortios-*', False),
    ('ecs-fortios-*,-ecs-microsoft-*', False),   # exclusions are ignored, not treated as targets
    ('ecs-fortios-*,ecs-microsoft-windows-v1', True),
])
def test_pack_overlaps(windows, index, expected):
    assert windows.overlaps(index) is expected


def test_repo_pack_summaries_include_retention_when_set():
    summaries = {p.name: p.summary() for p in load_packs(REPO_PACKS)}
    assert summaries['windows']['retention_days'] == 60 and summaries['fortios']['retention_days'] == 30
    assert 'retention_days' not in summaries['ingress_nginx']  # kept indefinitely
    assert summaries['windows']['event_type_field'] == 'event.code'
    assert summaries['fortios']['event_type_field'] == 'event.type_id'


async def test_pack_tool_notes_periods_older_than_retention(es, windows):
    from datetime import date, timedelta
    old = (date.today() - timedelta(days=75)).isoformat()
    recent = (date.today() - timedelta(days=10)).isoformat()
    async with Client(_server(es, [windows])) as c:
        stale = await c.call_tool('windows_user_events', {'user': 'a', 'time_range': {'start': old}})
        fresh = await c.call_tool('windows_user_events', {'user': 'a', 'time_range': {'start': recent}})
    assert 'every event for only 60 days' in stale.structured_content['note']
    assert 'note' not in fresh.structured_content


def test_distinct_values_tool_spec_needs_field():
    with pytest.raises(ValidationError):
        ToolSpec(name='t', kind='distinct_values', description='d')


async def test_distinct_values_tool_lists_logons_over_a_year(es, windows):
    es.search = AsyncMock(return_value={'aggregations': {'groups': {'buckets': [
        {'key': {'user.name': 'john.smith1'}, 'doc_count': 3,
         'first_seen': {'value_as_string': '2026-01-05T00:00:00Z'},
         'last_seen': {'value_as_string': '2026-09-01T00:00:00Z'},
         'sample': {'hits': {'hits': [{'_source': {'user': {'id': 'S-1-5-21-1', 'domain': 'intranet'}}}]}}}]}}})
    async with Client(_server(es, [windows])) as c:
        result = await c.call_tool('windows_logon_users',
                                   {'time_range': {'start': '2025-10-01', 'end': '2026-09-28'}})
    assert result.structured_content['values'] == [
        {'value': 'john.smith1', 'count': 3, 'first_seen': '2026-01-05T00:00:00Z',
         'last_seen': '2026-09-01T00:00:00Z', 'sample': {'user.id': 'S-1-5-21-1', 'user.domain': 'intranet'}}]
    assert 'only 60 days' in result.structured_content['note']
    clauses = es.search.call_args.kwargs['query']['bool']['filter']
    assert {'term': {'event.code': '4624'}} in clauses and {'term': {'user.type': 'User'}} in clauses


async def test_pack_search_tools_keep_the_event_search_limit(es, windows):
    async with Client(_server(es, [windows])) as c:
        with pytest.raises(ToolError, match='maximum of 90 days'):
            await c.call_tool('windows_user_events',
                              {'user': 'a', 'time_range': {'start': '2025-10-01', 'end': '2026-09-28'}})
