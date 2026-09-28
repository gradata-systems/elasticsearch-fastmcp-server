"""Source-agnostic tools that work against any index the caller may read."""
import fnmatch
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from tools.query import Filter, TimeRange, build_query, field_value, fit_to_budget
from utils.elasticsearch import ElasticsearchGateway, gateway_from, shard_failure

Index = Annotated[str, Field(
    description="Index, alias, data stream or pattern from list_data_sources, e.g. 'ecs-microsoft-windows-*'. "
                "For a known data source, use its 'index' pattern so every retention tier is searched.")]
TimestampField = Annotated[str, Field(description="Timestamp field used for the time range.")]
Filters = Annotated[list[Filter], Field(description="Exact-match and range conditions, all of which must hold.")]
QueryString = Annotated[str | None, Field(
    description="Optional full-text query in Lucene syntax, e.g. 'message:\"failed password\"' or "
                "'event.action:logon-failed AND NOT user.name:svc_*'. Leading wildcards are not allowed.")]

_TRUNCATED_HINT = "Output was truncated; narrow the time range, add filters, or request fewer fields."


def _with_shard_warning(result: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if reason := shard_failure(body):
        result['warning'] = f"Results are incomplete because part of the data could not be searched: {reason}"
    return result


async def run_search(es: ElasticsearchGateway, index: str, query: dict[str, Any], fields: list[str] | None,
                     size: int, sort: str, timestamp_field: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        'query': query,
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
    return _with_shard_warning(result, body)


async def run_top_values(es: ElasticsearchGateway, index: str, query: dict[str, Any], field: str, size: int,
                         include_fields: list[str] | None = None) -> dict[str, Any]:
    terms: dict[str, Any] = {'terms': {'field': field, 'size': size}}
    if include_fields:
        terms['aggs'] = {'sample': {'top_hits': {'size': 1, '_source': include_fields}}}
    body = await es.search(index, size=0, query=query, aggs={'top': terms}, track_total_hits=True)

    agg = body['aggregations']['top']
    values = []
    for bucket in agg['buckets']:
        value = {'value': bucket.get('key_as_string', bucket['key']), 'count': bucket['doc_count']}
        if include_fields:
            hits = bucket['sample']['hits']['hits']
            source = hits[0].get('_source', {}) if hits else {}
            value['sample'] = {f: field_value(source, f) for f in include_fields}
        values.append(value)
    result: dict[str, Any] = {
        'total_events': body['hits']['total']['value'],
        'values': values,
        'events_with_other_values': agg.get('sum_other_doc_count', 0),
    }
    if result['total_events'] and not values:
        result['hint'] = (f"None of the matching events have a value for '{field}'; it may not exist in this "
                          f"index. Use describe_fields to find the right field.")
    return _with_shard_warning(result, body)


async def list_data_sources(ctx: Context) -> dict[str, Any]:
    """
    List the indices, aliases and data streams you are permitted to query, and describe the known
    data sources among them (what they contain, key fields, and dedicated tools).
    Start here to find the index names to pass to the other tools.
    """
    es = gateway_from(ctx)
    body = await es.resolve_accessible()
    result: dict[str, Any] = {
        'indices': sorted(i['name'] for i in body.get('indices', []) if 'data_stream' not in i),
        'aliases': sorted(a['name'] for a in body.get('aliases', [])),
        'data_streams': sorted(d['name'] for d in body.get('data_streams', [])),
    }
    readable = set(result['indices']) | set(result['aliases']) | set(result['data_streams'])
    result['sources'] = [pack.summary() for pack in ctx.lifespan_context.get('packs', [])
                         if any(fnmatch.fnmatchcase(name, pack.index) for name in readable)]
    return result


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
    body = await es.field_caps(index, [f.strip() for f in fields.split(',') if f.strip()] or ['*'])
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
    return await run_search(
        es, index, build_query(time_range, timestamp_field, es.settings.max_time_range_days, filters, query),
        fields, size, sort, timestamp_field)


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
        include_fields: Annotated[list[str] | None, Field(
            description="Fields to show from one example event per value, e.g. ['user.id'] alongside "
                        "user.name.")] = None,
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    Return the most frequent values of a field and how many events have each, over a time range.
    Useful for triage: top users, source IPs, event codes, URLs, destination ports and so on.
    """
    es = gateway_from(ctx)
    return await run_top_values(
        es, index, build_query(time_range, timestamp_field, es.settings.max_time_range_days, filters, query),
        field, size, include_fields)


async def esql_query(
        query: Annotated[str, Field(
            description="ES|QL query starting with FROM, e.g. "
                        "'FROM ecs-ingress-nginx-access-* | WHERE http.response.status_code >= 500 "
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
