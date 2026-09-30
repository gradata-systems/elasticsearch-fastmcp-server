import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from config import Settings
from security import audit
from sources.packs import SourcePack, ToolSpec, load_packs
from tools import deployment

REPO_PACKS = Path(__file__).parent.parent / 'packs'
DAY = {'start': '2026-09-01', 'end': '2026-09-01'}


def _settings(prefix='', name='', description=''):
    return SimpleNamespace(tool_prefix=prefix, cluster_name=name, cluster_description=description,
                           max_result_size=500, max_response_chars=100_000, max_time_range_days=90,
                           max_aggregation_range_days=366)


def _server(settings, packs):
    es = MagicMock()
    es.settings = settings

    @asynccontextmanager
    async def lifespan(server):
        yield {'es': es, 'packs': packs}

    mcp = FastMCP('t', lifespan=lifespan, instructions=deployment.server_instructions(settings, packs))
    deployment.register(mcp, settings, packs)
    return mcp, es


def test_qualify_prefixes_whole_tool_names_only():
    text = "Use top_values, not my_top_values or top_values_x; see list_data_sources."
    assert deployment.qualify(text, ['top_values', 'list_data_sources'], 'prod_') == (
        "Use prod_top_values, not my_top_values or top_values_x; see prod_list_data_sources.")
    assert deployment.qualify(text, ['top_values'], '') == text


async def test_prefix_renames_tools_and_prompt_and_their_cross_references():
    mcp, _ = _server(_settings('prod_', 'Production SIEM'), load_packs(REPO_PACKS))
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        prompts = [p.name for p in await client.list_prompts()]
        prompt = await client.get_prompt('prod_create_source_pack', {'mapping': '{}'})

    assert all(name.startswith('prod_') for name in tools) and 'prod_windows_logon_users' in tools
    assert prompts == ['prod_create_source_pack']
    assert 'use prod_top_values instead' in tools['prod_search_events'].description
    assert tools['prod_search_events'].description.endswith('Cluster: Production SIEM.')
    # Parameter descriptions and pack YAML descriptions are rewritten too.
    assert 'from prod_list_data_sources' in tools['prod_top_values'].input_schema['properties']['index']['description']
    assert 'unlike prod_windows_active_users' in tools['prod_windows_logon_users'].description

    text = prompt.messages[0].content.text
    assert "this server's `prod_list_data_sources`" in text and '`prod_esql_query`' in text
    assert '- `top_values` when the answer is' in text  # a pack kind, not a tool name


async def test_without_prefix_or_cluster_nothing_changes():
    mcp, _ = _server(_settings(), load_packs(REPO_PACKS))
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert 'search_events' in tools and 'windows_logon_users' in tools
    assert 'Cluster:' not in tools['search_events'].description
    assert 'use top_values instead' in tools['search_events'].description


async def test_prefixed_pack_tools_still_run():
    mcp, es = _server(_settings('prod_'), [p for p in load_packs(REPO_PACKS) if p.name == 'windows'])
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 0}, 'hits': []}})
    es.field_caps = AsyncMock(return_value={'fields': {}})
    async with Client(mcp) as client:
        result = await client.call_tool('prod_windows_user_events', {'user': 'john.smith1', 'time_range': DAY})
    assert result.structured_content['total'] == 0
    assert es.search.call_args.args[0] == 'ecs-microsoft-windows-*'


def test_overlong_prefixed_names_are_rejected():
    pack = SourcePack(name='p', title='P', description='d', index='p-*',
                      tools=[ToolSpec(name='t' * 60, kind='search', description='d')])
    with pytest.raises(ValueError, match='longer than 64'):
        _server(_settings('cluster_'), [pack])


def test_tool_prefix_must_be_lower_snake_case_ending_in_underscore():
    required = dict(es_url='https://es:9200', es_impersonator_username='u', es_impersonator_password='p',
                    oidc_issuer='https://idp/realms/r', oidc_audience='a', public_base_url='https://m')
    assert Settings(**required, tool_prefix='prod_').tool_prefix == 'prod_'
    for bad in ('prod', 'Prod_', 'prod-'):
        with pytest.raises(ValidationError):
            Settings(**required, tool_prefix=bad)


@pytest.mark.parametrize('limit', ['max_result_size', 'max_time_range_days', 'max_aggregation_range_days',
                                   'max_response_chars', 'es_request_timeout'])
def test_limits_must_be_positive(limit):
    required = dict(es_url='https://es:9200', es_impersonator_username='u', es_impersonator_password='p',
                    oidc_issuer='https://idp/realms/r', oidc_audience='a', public_base_url='https://m')
    with pytest.raises(ValidationError):
        Settings(**required, **{limit: 0})


def test_instructions_name_the_cluster_and_say_to_ask_when_unclear():
    packs = load_packs(REPO_PACKS)
    text = deployment.server_instructions(_settings('prod_', 'Production SIEM', 'Security logs for the head office'),
                                          packs)
    assert text.startswith("This server queries the Elasticsearch cluster 'Production SIEM': Security logs for "
                           "the head office.")
    assert 'Source packs (known data sources) on this cluster: ' in text
    assert 'Microsoft Windows security events (ecs-microsoft-windows-*)' in text
    assert 'Start with prod_list_data_sources' in text
    assert 'never by counting rows yourself' in text and 'use prod_top_values, prod_distinct_values' in text
    assert 'relates two data sources' in text and 'prod_match_values does both in one call' in text
    # The cluster is chosen by the source packs the caller can read, never by unpacked indices.
    assert "the 'sources' in its prod_list_data_sources result" in text
    assert 'that no source pack describes' in text and 'ask the user which cluster they mean' in text

    single = deployment.server_instructions(_settings(), packs)
    assert single.startswith('This server queries an Elasticsearch cluster.')
    assert 'other clusters' not in single


async def test_list_data_sources_names_the_cluster_and_prefixed_tools():
    mcp, es = _server(_settings('prod_', 'Production SIEM', 'Head office'),
                      [p for p in load_packs(REPO_PACKS) if p.name == 'windows'])
    es.resolve_accessible = AsyncMock(return_value={
        'indices': [{'name': 'ecs-microsoft-windows-v1'}], 'aliases': [], 'data_streams': []})
    async with Client(mcp) as client:
        result = (await client.call_tool('prod_list_data_sources', {})).structured_content
    hint = result['cluster']['hint']
    assert result['cluster']['name'] == 'Production SIEM'
    assert "only by the source packs in each one's 'sources'" in hint and 'ask the user' in hint
    assert 'prod_windows_logon_users' in result['sources'][0]['tools']


async def test_top_values_hint_uses_the_prefixed_tool_name():
    mcp, es = _server(_settings('prod_'), [])
    es.search = AsyncMock(return_value={'hits': {'total': {'value': 5}}, 'aggregations': {'top': {'buckets': []}}})
    es.field_caps = AsyncMock(return_value={'fields': {'f': {'keyword': {'aggregatable': True}},
                                                       '@timestamp': {'date': {'aggregatable': True}}}})
    async with Client(mcp) as client:
        result = await client.call_tool('prod_top_values', {'index': 'i', 'field': 'f', 'time_range': DAY})
        with pytest.raises(ToolError, match='Use prod_describe_fields'):
            await client.call_tool('prod_top_values', {'index': 'i', 'field': 'missing', 'time_range': DAY})
    assert 'Use prod_describe_fields' in result.structured_content['hint']


def test_audit_events_name_the_cluster(capsys):
    audit.configure_audit_log(None, 'Production SIEM')
    try:
        audit.audit('tool_call', tool='t')
        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert record['cluster'] == 'Production SIEM'
    finally:
        audit.configure_audit_log(None)
        logging.getLogger('audit').handlers.clear()


async def test_tool_schemas_show_the_servers_result_size_limit():
    settings = _settings()
    settings.max_result_size = 50
    mcp, _ = _server(settings, load_packs(REPO_PACKS))
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools['distinct_values'].input_schema['properties']['size'] == {
        **tools['distinct_values'].input_schema['properties']['size'], 'maximum': 50, 'default': 50}
    assert tools['search_events'].input_schema['properties']['size']['maximum'] == 50
    assert tools['windows_logon_users'].input_schema['properties']['size']['maximum'] == 50


def test_no_source_packs_unless_configured():
    required = dict(es_url='https://es:9200', es_impersonator_username='u', es_impersonator_password='p',
                    oidc_issuer='https://idp/realms/r', oidc_audience='a', public_base_url='https://m')
    assert Settings(**required, _env_file=None).packs_dir is None
