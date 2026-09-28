"""Source-agnostic tools that work against any index the caller may read."""
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from tools.query import Filter, TimeRange, build_query, fit_to_budget
from utils.elasticsearch import gateway_from

Index = Annotated[str, Field(
    description="Index, alias or data stream name from list_data_sources, e.g. 'ecs-microsoft-windows-v1'. "
                "Wildcards are allowed within your permitted patterns.")]
TimestampField = Annotated[str, Field(description="Timestamp field used for the time range.")]
Filters = Annotated[list[Filter], Field(description="Exact-match and range conditions, all of which must hold.")]
QueryString = Annotated[str | None, Field(
    description="Optional full-text query in Lucene syntax, e.g. 'message:\"failed password\"' or "
                "'event.action:logon-failed AND NOT user.name:svc_*'. Leading wildcards are not allowed.")]

_TRUNCATED_HINT = "Output was truncated; narrow the time range, add filters, or request fewer fields."


async def list_data_sources(ctx: Context) -> dict[str, Any]:
    """
    List the indices, aliases and data streams you are permitted to query.
    Start here to find the index names to pass to the other tools.
    """
    es = gateway_from(ctx)
    body = await es.resolve_accessible()
    return {
        'indices': sorted(i['name'] for i in body.get('indices', []) if 'data_stream' not in i),
        'aliases': sorted(a['name'] for a in body.get('aliases', [])),
        'data_streams': sorted(d['name'] for d in body.get('data_streams', [])),
    }


async def describe_fields(
        index: Index,
        ctx: Context,
        fields: Annotated[str, Field(
            description="Comma-separated field name patterns to describe, e.g. 'user.*,source.ip'. "
                        "Use '*' for all fields (can be large for ECS data).")] = '*',
) -> dict[str, Any]:
    """
    Describe the fields available in an index and their types (keyword, text, ip, date, long, ...).
    Use this before searching to learn field names and whether to filter exactly (keyword, ip, numeric)
    or with a full-text query (text).
    """
    es = gateway_from(ctx)
    body = await es.field_caps(index, fields)
    described = {}
    for name, types in sorted(body.get('fields', {}).items()):
        kinds = sorted(t for t in types if t not in ('object', 'nested'))
        if name.startswith('_') or not kinds:
            continue
        described[name] = kinds[0] if len(kinds) == 1 else kinds
    items, truncated = fit_to_budget(list(described.items()), es.settings.max_response_chars)
    result: dict[str, Any] = {'indices': body.get('indices', []), 'fields': dict(items)}
    if truncated:
        result['truncated'] = True
        result['hint'] = "Too many fields to list; pass a narrower 'fields' pattern such as 'user.*'."
    return result


async def search_events(
        index: Index,
        time_range: TimeRange,
        ctx: Context,
        filters: Filters = [],
        query: QueryString = None,
        fields: Annotated[list[str] | None, Field(
            description="Fields to return for each event, e.g. ['@timestamp', 'user.name', 'source.ip']. "
                        "Wildcards allowed. Omit to return whole events.")] = None,
        size: Annotated[int, Field(ge=1, le=500, description="Maximum number of events to return.")] = 20,
        sort: Annotated[Literal['desc', 'asc'], Field(
            description="'desc' for most recent first, 'asc' for chronological order.")] = 'desc',
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    Search for individual events in a time range, optionally narrowed by exact-match filters and a
    full-text query. Returns the total number of matches and up to `size` events.
    To count or rank values (e.g. top source IPs), use top_values instead.
    """
    es = gateway_from(ctx)
    params: dict[str, Any] = {
        'query': build_query(time_range, timestamp_field, es.settings.max_time_range_days, filters, query),
        'size': size,
        'sort': [{timestamp_field: {'order': sort, 'unmapped_type': 'date'}}],
        'track_total_hits': True,
    }
    if fields:
        params['_source'] = fields
    body = await es.search(index, **params)

    events = [{'index': h['_index'], 'id': h['_id'], 'event': h.get('_source', {})} for h in body['hits']['hits']]
    events, truncated = fit_to_budget(events, es.settings.max_response_chars)
    result: dict[str, Any] = {'total': body['hits']['total']['value'], 'returned': len(events), 'events': events}
    if truncated:
        result['truncated'] = True
        result['hint'] = _TRUNCATED_HINT
    return result


async def top_values(
        index: Index,
        field: Annotated[str, Field(
            description="Field to rank values of. Must be keyword, numeric, ip, boolean or date, not text, "
                        "e.g. 'user.name', 'source.ip', 'event.code'.")],
        time_range: TimeRange,
        ctx: Context,
        filters: Filters = [],
        query: QueryString = None,
        size: Annotated[int, Field(ge=1, le=100, description="Number of top values to return.")] = 10,
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    Return the most frequent values of a field and how many events have each, over a time range.
    Useful for triage: top users, source IPs, event codes, URLs, destination ports and so on.
    """
    es = gateway_from(ctx)
    body = await es.search(
        index,
        size=0,
        query=build_query(time_range, timestamp_field, es.settings.max_time_range_days, filters, query),
        aggs={'top': {'terms': {'field': field, 'size': size}}},
        track_total_hits=True,
    )
    agg = body['aggregations']['top']
    return {
        'total_events': body['hits']['total']['value'],
        'values': [{'value': b.get('key_as_string', b['key']), 'count': b['doc_count']} for b in agg['buckets']],
        'events_with_other_values': agg.get('sum_other_doc_count', 0),
    }


async def esql_query(
        query: Annotated[str, Field(
            description="ES|QL query starting with FROM, e.g. "
                        "'FROM ecs-nginx-* | WHERE http.response.status_code >= 500 "
                        "| STATS errors = COUNT(*) BY url.path | SORT errors DESC | LIMIT 20'.")],
        time_range: TimeRange,
        ctx: Context,
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    Run a read-only ES|QL query for analysis the other tools can't express: grouping by several fields,
    computed columns, joins between conditions, time bucketing (BUCKET) and so on.
    The time range is applied automatically; don't repeat it in the query. Always end with LIMIT,
    and prefer STATS over returning raw rows.
    """
    es = gateway_from(ctx)
    time_filter = time_range.to_query(timestamp_field, es.settings.max_time_range_days)
    body = await es.esql(query, time_filter)

    columns = [c['name'] for c in body.get('columns', [])]
    rows = [dict(zip(columns, values)) for values in body.get('values', [])]
    limited = rows[:es.settings.max_result_size]
    limited, truncated = fit_to_budget(limited, es.settings.max_response_chars)
    result: dict[str, Any] = {'columns': columns, 'returned': len(limited), 'rows': limited}
    if truncated or len(limited) < len(rows):
        result['truncated'] = True
        result['hint'] = "Output was truncated; add STATS to aggregate or a smaller LIMIT."
    return result


ALL_TOOLS = [list_data_sources, describe_fields, search_events, top_values, esql_query]
