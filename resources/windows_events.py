import logging
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, field_validator, Field

from utils.elasticsearch import ElasticsearchGateway

logger = logging.getLogger(__name__)


class DateRange(BaseModel):
    start_date: Optional[str] = Field(description="The start date, inclusive, in YYYY-MM-DD format.")
    end_date: Optional[str] = Field(description="The end date, inclusive, in YYYY-MM-DD format.")

    @field_validator('start_date', 'end_date')
    def validate_date_format(cls, value):
        if value is None:
            return value
        try:
            datetime.strptime(value, '%Y-%m-%d')
            return value
        except ValueError:
            raise ValueError("Invalid date format. Use YYYY-MM-DD")


async def get_users(es: ElasticsearchGateway, index: str, date_range: DateRange) -> dict[str, Any]:
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

    response = await es.search(index, size=0, query=query, aggs=aggs)

    users = []
    for bucket in response['aggregations']['users']['buckets']:
        source = bucket['metadata']['hits']['hits'][0]['_source']
        users.append({
            "id": (source.get('user') or {}).get('id'),
            "login_name": bucket['key'],
            "event_count": bucket['doc_count']
        })

    return {"users": users}


async def get_top_events_by_user(es: ElasticsearchGateway, index: str, user: str, size: int,
                                 date_range: DateRange) -> dict[str, Any]:
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

    response = await es.search(index, size=size, query=query)
    return {"events": [hit['_source'] for hit in response['hits']['hits']]}


async def get_remote_access_events_by_user(es: ElasticsearchGateway, index: str, user: str, size: int,
                                           date_range: DateRange) -> dict[str, Any]:
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

    response = await es.search(index, size=size, query=query, sort=sort)
    return {"events": [hit['_source'] for hit in response['hits']['hits']]}
