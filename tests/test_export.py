import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ResourceError

from tools import deployment
from tools.export import ExportSpec, render
from tools.query import Filter, TimeRange

TR = {'start': '2026-09-01T00:00:00Z', 'end': '2026-09-02T00:00:00Z'}


def _server(prefix=''):
    es = MagicMock()
    es.settings = SimpleNamespace(tool_prefix=prefix, cluster_name='', cluster_description='', max_result_size=500,
                                  max_export_rows=10_000, max_response_chars=100_000, max_time_range_days=90,
                                  max_aggregation_range_days=366)
    es.field_caps = AsyncMock(return_value={'indices': ['i'], 'fields': {
        '@timestamp': {'date': {'type': 'date', 'aggregatable': True}},
        'user.name': {'keyword': {'type': 'keyword', 'aggregatable': True}}}})

    @asynccontextmanager
    async def lifespan(server):
        yield {'es': es, 'packs': []}

    mcp = FastMCP('t', lifespan=lifespan)
    deployment.register(mcp, es.settings, [])
    return mcp, es


def _hits(total, sources):
    return {'hits': {'total': {'value': total}, 'hits': [{'_source': s} for s in sources]}}


async def test_export_link_reruns_the_search_and_returns_csv():
    mcp, es = _server('prod_')
    es.search = AsyncMock(side_effect=[
        _hits(12_000, []),
        _hits(12_000, [{'@timestamp': '2026-09-01T01:00:00Z', 'user': {'name': 'alice'}, 'tags': ['a', 'b']},
                       {'@timestamp': '2026-09-01T00:30:00Z', 'user': {'name': 'bob, jr'}}]),
    ])
    async with Client(mcp) as client:
        result = await client.call_tool('prod_export_events', {
            'index': 'logs-*', 'time_range': TR, 'fields': ['@timestamp', 'user.name', 'tags'],
            'filters': [{'field': 'user.name', 'op': 'exists'}]})
        link = next(c for c in result.content if isinstance(c, mt.ResourceLink))
        contents = await client.read_resource(link.uri)
        templates = await client.list_resource_templates()

    assert result.structured_content['total'] == 12_000 and result.structured_content['exported'] == 10_000
    assert 'most recent 10000 of 12000' in result.structured_content['hint']
    assert link.mime_type == 'text/csv' and link.name == 'logs-20260901-20260902.csv'
    assert str(link.uri) == result.structured_content['uri']
    assert contents[0].text == ('@timestamp,user.name,tags\n'
                                '2026-09-01T01:00:00Z,alice,"[""a"", ""b""]"\n'
                                '2026-09-01T00:30:00Z,"bob, jr",\n')
    assert contents[0].mime_type == 'text/csv'
    assert [t.name for t in templates] == ['prod_export']

    count, export = (call.kwargs for call in es.search.call_args_list)
    assert count['size'] == 0 and export['query'] == count['query']
    assert export['size'] == 10_000 and export['limit'] == 10_000
    assert export['_source'] == ['@timestamp', 'user.name', 'tags']


async def test_export_fixes_an_open_ended_period_and_exports_whole_events_as_ndjson():
    mcp, es = _server()
    es.search = AsyncMock(side_effect=[_hits(1, []), _hits(1, [{'user': {'name': 'alice'}}])])
    async with Client(mcp) as client:
        result = await client.call_tool('export_events', {'index': 'logs-*', 'time_range': {'start': '2026-09-01'}})
        spec = ExportSpec.decode(str(result.structured_content['uri']).split('/')[-2])
        contents = await client.read_resource(result.structured_content['uri'])
    assert spec.time_range.end is not None
    assert result.structured_content['format'] == 'ndjson' and 'hint' not in result.structured_content
    assert contents[0].text == '{"user.name": "alice"}\n'


def test_spec_round_trips_and_rejects_tampered_links():
    spec = ExportSpec(index='a-*,-a-debug-*', time_range=TimeRange(**TR), query='user.name:x*',
                      filters=[Filter(field='f', op='in', value=[1, 2])], fields=['f'])
    assert ExportSpec.decode(spec.encode()) == spec
    for bad in ['not-base64!', spec.encode()[:-4], 'eJwLyczPy1dIzs8rSc0rUSjJLy1RBAA']:
        with pytest.raises(ResourceError):
            ExportSpec.decode(bad)


def test_render_uses_csv_only_for_exactly_named_fields():
    sources = [{'user': {'name': 'a'}, 'n': 1}]
    wildcard = ExportSpec(index='i', time_range=TimeRange(**TR), fields=['user.*'])
    assert json.loads(render(wildcard, sources)) == {'user.name': 'a', 'n': 1}
    assert render(ExportSpec(index='i', time_range=TimeRange(**TR), fields=['n']), sources) == 'n\n1\n'
