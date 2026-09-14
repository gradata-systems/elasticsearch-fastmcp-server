import json
import os
from contextlib import asynccontextmanager

from elasticsearch import AsyncElasticsearch

es_host = 'https://prod-es-data-http.elastic.svc.prod:9200'
es_api_key = os.getenv('ES_MCP_SERVER_API_KEY')
index_windows_events = 'ecs-microsoft-windows-v1'


@asynccontextmanager
async def get_es_client():
    client = AsyncElasticsearch([es_host], api_key=es_api_key)
    try:
        yield client
    finally:
        await client.close()


async def query_elasticsearch(index: str, **params):
    """Makes a request to Elasticsearch with proper error handling.

    Search parameters (query, aggs, size, sort, ...) are passed through as
    keyword arguments to the client.
    """
    print(f"Sending query to Elasticsearch: {json.dumps(params)}")

    # Use context manager
    async with get_es_client() as client:
        try:
            response = await client.search(
                index=index,
                **params
            )
            return response
        except Exception as e:
            print(f"Error querying Elasticsearch: {e}")
            return None
