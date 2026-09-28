import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)

READ_PRIVILEGES = ['read', 'view_index_metadata']


@dataclass(frozen=True)
class _CachedKey:
    encoded: str
    expires_at: float


class ApiKeyBroker:
    """Mints short-lived Elasticsearch API keys limited to a set of index patterns.

    Keys are cached per distinct set of index patterns, so callers with equivalent access
    share a key. Elasticsearch caps a derived key's privileges at the intersection of its
    role descriptors and the broker user's own privileges, so the index boundary is
    enforced by Elasticsearch rather than by this server.
    """

    def __init__(self, client: AsyncElasticsearch, lifetime_seconds: int, refresh_margin_seconds: int = 300):
        if lifetime_seconds <= refresh_margin_seconds:
            raise ValueError("API key lifetime must exceed the refresh margin")
        self._client = client
        self._lifetime = lifetime_seconds
        self._margin = refresh_margin_seconds
        self._cache: dict[tuple[str, ...], _CachedKey] = {}
        self._lock = asyncio.Lock()

    async def key_for(self, index_patterns: frozenset[str]) -> str:
        """Return an encoded API key that can read only `index_patterns`."""
        # An empty role_descriptors would give the key the broker's full privileges.
        if not index_patterns:
            raise PermissionError("No index patterns granted; refusing to create an API key")

        cache_key = tuple(sorted(index_patterns))
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at - self._margin > time.time():
            return cached.encoded

        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at - self._margin > time.time():
                return cached.encoded

            digest = hashlib.sha256('\n'.join(cache_key).encode()).hexdigest()[:16]
            response = await self._client.security.create_api_key(
                name=f'mcp-{digest}',
                expiration=f'{self._lifetime}s',
                role_descriptors={
                    'mcp_read': {
                        'cluster': [],
                        'indices': [{'names': list(cache_key), 'privileges': READ_PRIVILEGES}],
                    }
                },
                metadata={'application': 'elasticsearch-fastmcp-server', 'index_patterns': list(cache_key)},
            )
            expires_at = response.get('expiration', (time.time() + self._lifetime) * 1000) / 1000
            self._cache[cache_key] = _CachedKey(response['encoded'], expires_at)
            logger.info("Created API key %s for index patterns %s", response['id'], list(cache_key))
            return response['encoded']
