import fnmatch
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastmcp.exceptions import ToolError

from prompts.source_pack import SKELETON
from sources.packs import SourcePack
from tools.pack_validation import validate_source_pack

FIELDS = {
    '@timestamp': {'date': {'aggregatable': True}},
    'account': {'keyword': {'aggregatable': True}},
    'action': {'keyword': {'aggregatable': True}},
    'outcome': {'keyword': {'aggregatable': True}},
    'message': {'text': {'aggregatable': False}},
}


@pytest.fixture
def es():
    def field_caps(index, patterns):
        return {'indices': ['app-audit-1'],
                'fields': {f: c for f, c in FIELDS.items() if any(fnmatch.fnmatchcase(f, p) for p in patterns)}}
    return SimpleNamespace(settings=SimpleNamespace(tool_prefix=''), field_caps=AsyncMock(side_effect=field_caps))


def _ctx(es, packs=()):
    return SimpleNamespace(lifespan_context={'es': es, 'packs': list(packs)})


def _pack(**changes) -> str:
    data = yaml.safe_load(SKELETON) | changes
    return yaml.safe_dump(data, sort_keys=False)


async def test_skeleton_is_valid(es):
    assert await validate_source_pack(SKELETON, _ctx(es)) == {'valid': True, 'problems': []}


async def test_json_is_refused(es):
    result = await validate_source_pack(json.dumps(yaml.safe_load(SKELETON)), _ctx(es))
    assert result == {'valid': False, 'problems': ["The pack is JSON; write it as YAML, the format of the pack files."]}


async def test_misnamed_and_misshapen_keys_are_explained(es):
    data = yaml.safe_load(SKELETON)
    data['fields'] = [{'name': 'account', 'description': 'Account'}]
    data['default_fields'] = {'account': 'x'}
    data['tools'][0]['params']['account']['field'] = ['account']
    data['tools'][1]['filter'] = []
    result = await validate_source_pack(yaml.safe_dump(data), _ctx(es))
    assert not result['valid']
    assert result['problems'] == [
        'default_fields: Input should be a valid list (a list of items, not a map)',
        'tools[0].params.account.field: unknown key; did you mean fields?',
        'tools[1].filter: unknown key; did you mean filters?',
        'fields: unknown key; did you mean key_fields or default_fields?',
    ]


async def test_key_fields_as_a_list_is_explained(es):
    result = await validate_source_pack(_pack(key_fields=[{'account': 'Account'}]), _ctx(es))
    assert result['problems'] == ['key_fields: Input should be a valid dictionary (a map of name: value, not a list)']


@pytest.mark.parametrize('text, problem', [
    ('name: [unclosed', 'Not valid YAML'),
    ('- a list', 'must be a YAML map'),
])
async def test_unparseable_packs(es, text, problem):
    assert problem in (await validate_source_pack(text, _ctx(es)))['problems'][0]


async def test_fields_are_checked_against_the_data(es):
    data = yaml.safe_load(SKELETON)
    data['key_fields']['acount'] = 'typo'
    data['tools'][1]['field'] = 'message'
    data['tools'][0]['query'] = 'action:login or action:logout'
    result = await validate_source_pack(yaml.safe_dump(data), _ctx(es))
    assert result['problems'] == [
        "tools 'app_audit_account_events' query: Lucene searches for a lowercase 'or' as a word; write OR for the "
        "operator, or quote it to search for the word.",
        "No field 'acount' in 'app-audit-*,app-audit-archive'. Did you mean account?",
        "'message' can't be grouped by; choose a keyword, numeric, ip, boolean or date field.",
    ]


async def test_unreadable_index_is_noted_not_failed(es):
    es.field_caps = AsyncMock(side_effect=ToolError("Index 'x' is not available through this server"))
    result = await validate_source_pack(SKELETON, _ctx(es))
    assert result['valid'] and "weren't checked against the data" in result['notes'][0]


async def test_tool_names_must_be_free_and_unique(es):
    data = yaml.safe_load(SKELETON)
    data['tools'][1]['name'] = 'top_values'
    data['tools'][2]['name'] = data['tools'][0]['name']
    other = SourcePack(name='other', title='O', description='d', index='o',
                       tools=[{'name': 'app_audit_top_accounts', 'kind': 'search', 'description': 'd'}])
    result = await validate_source_pack(yaml.safe_dump(data), _ctx(es, [other]))
    assert result['problems'] == ["tools: 'top_values' is already the name of a tool on this server.",
                                  "tools: 'app_audit_account_events' is defined twice."]


async def test_a_revised_pack_may_keep_its_tool_names(es):
    loaded = SourcePack.model_validate(yaml.safe_load(SKELETON))
    assert (await validate_source_pack(SKELETON, _ctx(es, [loaded])))['valid']
