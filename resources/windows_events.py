import json
import logging
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator, Field

from utils.elasticsearch import query_elasticsearch, index_windows_events

logger = logging.getLogger(__name__)


class DateRange(BaseModel):
    start_date: Optional[str] = Field(description="Start date, inclusive, in YYYY-MM-DD format")
    end_date: Optional[str] = Field(description="End date, inclusive, in YYYY-MM-DD format")

    @field_validator('start_date', 'end_date')
    def validate_date_format(cls, value):
        if value is None:
            return value
        try:
            datetime.strptime(value, '%Y-%m-%d')
            return value
        except ValueError:
            raise ValueError("Invalid date format. Use YYYY-MM-DD")


async def get_users(date_range: DateRange) -> str:
    query = {
        "bool": {
            "must": [
                {
                    "range": {
                        "@timestamp": {
                            "gte": date_range.start_date,
                            "lte": date_range.end_date
                        }
                    }
                },
                {
                    "term": {
                        "user.type": {
                            "value": "User"
                        }
                    }
                }
            ]
        }
    }

    aggs = {
        "users": {
            "terms": {
                "size": 100,
                "field": "user.name"
            },
            "aggs": {
                "metadata": {
                    "top_hits": {
                        "size": 1,
                        "_source": ["user.id"]
                    }
                }
            }
        }
    }

    print(f'Querying Windows events from {index_windows_events}')
    response = await query_elasticsearch(index_windows_events, size=0, query=query, aggs=aggs)
    if not response:
        return json.dumps({
            "error": f"Failed to query Elasticsearch index {index_windows_events}"
        }, indent=2)

    users = []
    for bucket in response['aggregations']['users']['buckets']:
        users.append({
            "id": bucket['metadata']['hits']['hits'][0]['_source'].get('user.id'),
            "login_name": bucket['key'],
            "event_count": bucket['doc_count']
        })

    return json.dumps({
        "users": users
    }, indent=2)


async def get_top_events_by_user(user: str, size: int, date_range: DateRange) -> str:
    query = {
        "bool": {
            "must": [
                {
                    "range": {
                        "@timestamp": {
                            "gte": date_range.start_date,
                            "lte": date_range.end_date
                        }
                    }
                },
                {
                    "term": {
                        "user.name": {
                            "value": user
                        }
                    }
                }
            ]
        }
    }

    response = await query_elasticsearch(index_windows_events, size=size, query=query)
    if not response:
        return json.dumps({
            "error": f"Failed to query Elasticsearch index {index_windows_events}"
        }, indent=2)

    events = []
    for hit in response['hits']['hits']:
        events.append(hit['_source'])

    return json.dumps({
        "events": events
    })


async def get_remote_access_events_by_user(user: str, size: int, date_range: DateRange) -> str:
    query = {
        "bool": {
            "must": [
                {
                    "range": {
                        "@timestamp": {
                            "gte": date_range.start_date,
                            "lte": date_range.end_date
                        }
                    }
                },
                {
                    "bool": {
                        "should": [
                            {
                                "term": {
                                    "user.id": {
                                        "value": user
                                    }
                                }
                            },
                            {
                                "term": {
                                    "user.name": {
                                        "value": user
                                    }
                                }
                            }
                        ]
                    }
                },
                {
                    "terms": {
                        "event.provider": [
                            "Microsoft-Windows-TerminalServices-Gateway",
                            "Microsoft-Windows-TerminalServices-RemoteConnectionManager"
                        ]
                    }
                }
            ]
        }
    }

    sort = [
        {
            "@timestamp": {
                "order": "asc"
            }
        }
    ]

    response = await query_elasticsearch(index_windows_events, size=size, query=query, sort=sort)
    if not response:
        return json.dumps({
            "error": f"Failed to query Elasticsearch index {index_windows_events}"
        }, indent=2)

    events = []
    for hit in response['hits']['hits']:
        events.append(hit['_source'])

    return json.dumps({
        "events": events
    })
