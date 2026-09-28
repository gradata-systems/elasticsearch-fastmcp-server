import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from security.api_keys import ApiKeyBroker


def make_client(lifetime=3600):
    client = MagicMock()
    counter = iter(range(100))

    async def create_api_key(**kwargs):
        n = next(counter)
        return {'id': f'id{n}', 'encoded': f'key{n}', 'expiration': (time.time() + lifetime) * 1000}

    client.security.create_api_key = AsyncMock(side_effect=create_api_key)
    return client


async def test_refuses_empty_patterns():
    client = make_client()
    broker = ApiKeyBroker(client, lifetime_seconds=3600)
    with pytest.raises(PermissionError):
        await broker.key_for(frozenset())
    client.security.create_api_key.assert_not_called()


async def test_key_is_scoped_read_only_and_cached():
    client = make_client()
    broker = ApiKeyBroker(client, lifetime_seconds=3600)
    patterns = frozenset({'b-*', 'a-*'})

    assert await broker.key_for(patterns) == 'key0'
    assert await broker.key_for(frozenset({'a-*', 'b-*'})) == 'key0'
    assert client.security.create_api_key.call_count == 1

    kwargs = client.security.create_api_key.call_args.kwargs
    assert kwargs['role_descriptors'] == {
        'mcp_read': {'cluster': [], 'indices': [{'names': ['a-*', 'b-*'], 'privileges': ['read', 'view_index_metadata']}]}
    }
    assert kwargs['expiration'] == '3600s'


async def test_distinct_pattern_sets_get_distinct_keys():
    broker = ApiKeyBroker(make_client(), lifetime_seconds=3600)
    assert await broker.key_for(frozenset({'a-*'})) != await broker.key_for(frozenset({'b-*'}))


async def test_key_refreshed_near_expiry():
    client = make_client(lifetime=200)  # already inside the 300s refresh margin
    broker = ApiKeyBroker(client, lifetime_seconds=3600)
    await broker.key_for(frozenset({'a-*'}))
    assert await broker.key_for(frozenset({'a-*'})) == 'key1'
