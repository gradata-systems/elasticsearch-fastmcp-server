from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from tools.checks import check_fields, check_lucene, lucene_fields
from tools.query import Filter


@pytest.mark.parametrize('query, expected', [
    ('user.name:alice AND -host.name:x', {'user.name', 'host.name'}),
    ('(event.code:4625 OR !source.ip:10.0.0.1) +@timestamp:[2026-01-01T10:00 TO *]', {'event.code', 'source.ip',
                                                                                        '@timestamp'}),
    ('message:"a b:c" url.original:http\\://x/y', {'message', 'url.original'}),
    ('_exists_:user.id AND _id:abc', {'user.id'}),
    ('failed password', set()),
])
def test_lucene_fields(query, expected):
    assert lucene_fields(query) == expected


@pytest.mark.parametrize('query, error', [
    ('user.name:alice and host.name:x', "write AND"),
    ('message:(error or warning)', "write OR"),
    ('not user.name:alice', "write NOT"),
    ('user.name == "alice"', "no '==' operator"),
    ('status_code >= 500', "no '>=' operator"),
    ('user.name=alice', "no '=' operator"),
])
def test_lucene_mistakes_are_rejected(query, error):
    with pytest.raises(ToolError, match=error):
        check_lucene(query)


@pytest.mark.parametrize('query', [
    'user.name:alice AND NOT host.name:x',
    'message:"rock and roll" OR url.query:a=b',
    'http.response.status_code:>=500',
    'user.name:and\\ or',
    None,
])
def test_valid_lucene_is_accepted(query):
    check_lucene(query)


def _caps(kind, aggregatable=True):
    return {kind: {'type': kind, 'aggregatable': aggregatable}}


FIELDS = {
    '@timestamp': _caps('date'),
    'event.created': _caps('keyword'),
    'user': _caps('object', False),
    'user.name': _caps('keyword'),
    'user.id': _caps('keyword'),
    'message': _caps('text', False),
    'process.command_line': _caps('text', False),
    'process.command_line.keyword': _caps('keyword'),
}


@pytest.fixture
def es():
    def field_caps(index, patterns):
        import fnmatch
        return {'fields': {f: c for f, c in FIELDS.items() if any(fnmatch.fnmatchcase(f, p) for p in patterns)}}
    return SimpleNamespace(settings=SimpleNamespace(tool_prefix='prod_'), field_caps=AsyncMock(side_effect=field_caps))


async def test_valid_fields_take_one_request(es):
    await check_fields(es, 'i', filters=[Filter(field='user.name', value='a'), Filter(field='message', op='exists')],
                       query='user.id:1', aggregated=['user.name'], timestamp_field='@timestamp')
    es.field_caps.assert_awaited_once()


async def test_wildcards_and_metadata_fields_are_not_checked(es):
    await check_fields(es, 'i', filters=[Filter(field='_id', value='x')], query='user.*:a')
    es.field_caps.assert_not_awaited()


@pytest.mark.parametrize('kwargs, error', [
    ({'filters': [Filter(field='User.Name', value='a')]},
     r"No field 'User.Name' in 'i'. Did you mean user.name\? Use prod_describe_fields"),
    ({'query': 'name:alice'}, r"No field 'name'.*Did you mean user.name\?"),
    ({'filters': [Filter(field='user', value='a')]}, r"'user' is an object.*e.g. user.id, user.name\."),
    ({'filters': [Filter(field='process.command_line', value='a')]},
     r"text field.*use 'process.command_line.keyword'"),
    ({'filters': [Filter(field='message', op='in', value=['a'])]}, r"text field.*full-text query"),
    ({'aggregated': ['message']}, r"'message' can't be grouped by; choose a keyword"),
    ({'timestamp_field': 'event.created'}, r"'event.created' is not a date field"),
])
async def test_field_mistakes_are_rejected(es, kwargs, error):
    with pytest.raises(ToolError, match=error):
        await check_fields(es, 'i', **kwargs)


async def test_text_fields_may_be_range_filtered_and_searched(es):
    await check_fields(es, 'i', filters=[Filter(field='message', op='gt', value='a')], query='message:error')
