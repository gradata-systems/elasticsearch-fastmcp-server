"""Source-agnostic tools that work against any index the caller may read."""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.esql import aggregates, quoted_field_names, source_indices
from security.policy import matches_index_expression
from tools.checks import aggregatable_fields, check_fields
from tools.query import Filter, TimeRange, bool_query, build_query, field_value, fit_to_budget, flatten
from utils.elasticsearch import ElasticsearchGateway, gateway_from, incomplete

Index = Annotated[str, Field(
    description="Index, alias, data stream or pattern from list_data_sources, e.g. 'ecs-microsoft-windows-*'. "
                "Comma-separate several targets and prefix one with '-' to exclude it, e.g. "
                "'ecs-fortios-*,-ecs-fortios-rs-*'. For a known data source, use its 'index' so every "
                "retention tier is searched.")]
TimestampField = Annotated[str, Field(description="Timestamp field used for the time range.")]
Filters = Annotated[list[Filter], Field(description="Exact-match and range conditions, all of which must hold.")]
QueryString = Annotated[str | None, Field(
    description="Optional full-text query in Lucene syntax, e.g. 'message:\"failed password\"' or "
                "'event.action:logon-failed AND NOT user.name:svc_*'. Leading wildcards are not allowed.")]

_TRUNCATED_HINT = "Output was truncated; narrow the time range, add filters, or request fewer fields."

# distinct_values and compare_periods page through every group with a composite aggregation, up to this many.
_PAGE_SIZE = 1000
_MAX_GROUPS = 10_000

# Searches for at least this many events also summarise every matching event, so that the model
# reads counts from Elasticsearch instead of counting rows itself.
SUMMARY_MIN_SIZE = 20
_SUMMARY_FIELDS = 6
_SUMMARY_VALUES = 5
MAX_SEARCH_SIZE = 100


def retention_note(packs: list[Any], index: str, start: datetime) -> str | None:
    """A caution when a period starting at `start` reaches back before a searched source keeps every event."""
    limited = [p for p in packs if p.retention_days and p.overlaps(index)]
    if not limited:
        return None
    pack = min(limited, key=lambda p: p.retention_days)
    complete_since = datetime.now(timezone.utc) - timedelta(days=pack.retention_days)
    if start >= complete_since:
        return None
    return (f"{pack.title} keeps every event for only {pack.retention_days} days (since "
            f"{complete_since.date().isoformat()}); before that it holds only a subset, so fewer or no events "
            f"there don't mean nothing happened.")


def _add_retention_note(result: dict[str, Any], ctx: Context, index: str, start: datetime) -> dict[str, Any]:
    if note := retention_note(ctx.lifespan_context.get('packs', []), index, start):
        result['note'] = note
    return result


def _with_shard_warning(result: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if reason := incomplete(body):
        result['warning'] = f"Results are incomplete because part of the data could not be searched: {reason}"
    return result


def _capped(es: ElasticsearchGateway, size: int) -> tuple[int, bool]:
    """`size` limited to the server's ES_MCP_MAX_RESULT_SIZE, and whether it was lowered."""
    limit = es.settings.max_result_size
    return min(size, limit), size > limit


def _cap_hint(es: ElasticsearchGateway) -> str:
    return (f"This server returns at most {es.settings.max_result_size} rows per call; add filters, narrow the "
            f"time range or aggregate rather than asking for more.")


async def summary_fields(es: ElasticsearchGateway, index: str, candidates: list[str | None], size: int,
                         timestamp_field: str) -> list[str]:
    """The fields a search of `size` events summarises: aggregatable `candidates`, other than the timestamp."""
    if size < SUMMARY_MIN_SIZE:
        return []
    names = [f for f in candidates if f and f != timestamp_field]
    return (await aggregatable_fields(es, index, names))[:_SUMMARY_FIELDS]


def _summary(body: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    aggs = body.get('aggregations', {})
    summary: dict[str, Any] = {
        'first_event': aggs.get('first', {}).get('value_as_string'),
        'last_event': aggs.get('last', {}).get('value_as_string'),
    }
    if fields:
        summary['top_values'] = {f: [[b.get('key_as_string', b['key']), b['doc_count']]
                                     for b in aggs.get(f'top_{i}', {}).get('buckets', [])]
                                 for i, f in enumerate(fields)}
    summary['note'] = (f"Covers all {body['hits']['total']['value']} matching events, not only the rows "
                       f"returned. Take counts from here and from 'total' rather than counting rows.")
    return summary


async def run_search(es: ElasticsearchGateway, index: str, query: dict[str, Any], fields: list[str] | None,
                     size: int, sort: str, timestamp_field: str, summarise: list[str] = ()) -> dict[str, Any]:
    """Search for events. With `fields` named exactly, returns them as a table of columns and rows;
    otherwise as flattened events. Searches for SUMMARY_MIN_SIZE or more events add a summary of every
    matching event, with the top values of the `summarise` fields."""
    size, capped = _capped(es, size)
    params: dict[str, Any] = {
        'query': query,
        'size': size,
        'sort': [{timestamp_field: {'order': sort, 'unmapped_type': 'date'}}],
        'track_total_hits': True,
    }
    if fields:
        params['_source'] = fields
    if size >= SUMMARY_MIN_SIZE:
        params['aggs'] = {'first': {'min': {'field': timestamp_field}}, 'last': {'max': {'field': timestamp_field}}}
        params['aggs'] |= {f'top_{i}': {'terms': {'field': f, 'size': _SUMMARY_VALUES}}
                           for i, f in enumerate(summarise)}
    body = await es.search(index, **params)

    sources = [h.get('_source', {}) for h in body['hits']['hits']]
    result: dict[str, Any] = {'total': body['hits']['total']['value']}
    if fields and not any('*' in f or '?' in f for f in fields):
        rows, truncated = fit_to_budget([[field_value(s, f) for f in fields] for s in sources],
                                        es.settings.max_response_chars)
        result |= {'returned': len(rows), 'columns': fields, 'rows': rows}
    else:
        events, truncated = fit_to_budget([flatten(s) for s in sources], es.settings.max_response_chars)
        result |= {'returned': len(events), 'events': events}
    if 'aggs' in params:
        result['summary'] = _summary(body, list(summarise))
    if truncated:
        result['truncated'] = True
        result['hint'] = _TRUNCATED_HINT
    elif capped and result['total'] > result['returned']:
        result['truncated'] = True
        result['hint'] = _cap_hint(es)
    return _with_shard_warning(result, body)


async def run_top_values(es: ElasticsearchGateway, index: str, query: dict[str, Any], field: str, size: int,
                         include_fields: list[str] | None = None) -> dict[str, Any]:
    size, capped = _capped(es, size)
    terms: dict[str, Any] = {'terms': {'field': field, 'size': size}}
    if include_fields:
        terms['aggs'] = {'sample': {'top_hits': {'size': 1, '_source': include_fields}}}
    body = await es.search(index, size=0, query=query, aggs={'top': terms}, track_total_hits=True)

    agg = body['aggregations']['top']
    rows, truncated = fit_to_budget(
        [[bucket.get('key_as_string', bucket['key']), bucket['doc_count'], *_sample(bucket, include_fields)]
         for bucket in agg['buckets']], es.settings.max_response_chars)
    result: dict[str, Any] = {
        'total_events': body['hits']['total']['value'],
        'events_with_other_values': agg.get('sum_other_doc_count', 0),
        'columns': [field, 'count', *(include_fields or [])],
        'rows': rows,
    }
    if truncated:
        result['truncated'] = True
        result['hint'] = "Output was truncated; ask for fewer values or fewer include_fields."
    elif capped and result['events_with_other_values']:
        result['truncated'] = True
        result['hint'] = _cap_hint(es)
    elif result['total_events'] and not rows:
        result['hint'] = (f"None of the matching events have a value for '{field}'; it may not exist in this "
                          f"index. Use {es.settings.tool_prefix}describe_fields to find the right field.")
    return _with_shard_warning(result, body)


def _sample(bucket: dict[str, Any], include_fields: list[str] | None) -> list[Any]:
    """The `include_fields` of the example event in a bucket's 'sample' aggregation."""
    if not include_fields:
        return []
    hits = bucket['sample']['hits']['hits']
    source = hits[0].get('_source', {}) if hits else {}
    return [field_value(source, f) for f in include_fields]


async def _composite_buckets(es: ElasticsearchGateway, index: str, query: dict[str, Any],
                             sources: list[dict[str, Any]], aggs: dict[str, Any]
                             ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Every bucket of a composite aggregation, page by page, up to _MAX_GROUPS.

    Returns the buckets, whether they are all of them, and the first page with a shard failure (or {}).
    """
    buckets, failed_page, after_key = [], {}, None
    while True:
        composite: dict[str, Any] = {'size': _PAGE_SIZE, 'sources': sources}
        if after_key:
            composite['after'] = after_key
        body = await es.search(index, size=0, query=query, aggs={'groups': {'composite': composite, 'aggs': aggs}})
        if not failed_page and incomplete(body):
            failed_page = body
        agg = body['aggregations']['groups']
        buckets += agg['buckets']
        if not agg['buckets'] or 'after_key' not in agg:
            return buckets, True, failed_page
        if len(buckets) >= _MAX_GROUPS:
            return buckets, False, failed_page
        after_key = agg['after_key']


# Sort keys for distinct_values rows: [value, count, first_seen, last_seen, *samples].
_DISTINCT_ORDER = {
    'last_seen': (lambda row: row[3] or '', True),
    'count': (lambda row: row[1], True),
    'value': (lambda row: str(row[0]), False),
}


async def run_distinct_values(es: ElasticsearchGateway, index: str, query: dict[str, Any], field: str, size: int,
                              timestamp_field: str, order: str = 'last_seen',
                              include_fields: list[str] | None = None) -> dict[str, Any]:
    size, capped = _capped(es, size)
    aggs: dict[str, Any] = {'first_seen': {'min': {'field': timestamp_field}},
                            'last_seen': {'max': {'field': timestamp_field}}}
    if include_fields:
        aggs['sample'] = {'top_hits': {'size': 1, '_source': include_fields}}
    buckets, complete, failed_page = await _composite_buckets(
        es, index, query, [{field: {'terms': {'field': field}}}], aggs)

    values = [[bucket['key'][field], bucket['doc_count'], bucket['first_seen'].get('value_as_string'),
               bucket['last_seen'].get('value_as_string'), *_sample(bucket, include_fields)]
              for bucket in buckets]
    key, reverse = _DISTINCT_ORDER[order]
    values.sort(key=key, reverse=reverse)
    rows, truncated = fit_to_budget(values[:size], es.settings.max_response_chars)

    result: dict[str, Any] = {'distinct_values': len(values), 'returned': len(rows),
                              'columns': [field, 'count', 'first_seen', 'last_seen', *(include_fields or [])],
                              'rows': rows}
    if not complete:
        result['distinct_values_complete'] = False
        result['hint'] = (f"There are more than {len(values)} distinct values and only that many were read; "
                          f"add filters or narrow the time range for a complete list.")
    elif truncated or len(rows) < len(values):
        result['truncated'] = True
        result['hint'] = _cap_hint(es) if capped and not truncated else (
            "More values exist than were returned; raise size, add filters or request fewer include_fields.")
    return _with_shard_warning(result, failed_page)


async def list_data_sources(ctx: Context) -> dict[str, Any]:
    """
    List the indices, aliases and data streams you are permitted to query, and describe the known
    data sources among them (what they contain, key fields, and dedicated tools).
    Start here to find the index names to pass to the other tools.
    """
    es = gateway_from(ctx)
    body = await es.resolve_accessible()
    result: dict[str, Any] = {}
    if es.settings.cluster_name:
        result['cluster'] = {
            'name': es.settings.cluster_name,
            'description': es.settings.cluster_description,
            'hint': "Other clusters may be available through other deployments of this server. Choose between "
                    "them only by the source packs in each one's 'sources', not by indices that no source pack "
                    "describes. If source packs on more than one cluster could hold what the user is asking "
                    "about, or none clearly does, ask the user which cluster they mean.",
        }
    result.update({
        'indices': sorted(i['name'] for i in body.get('indices', []) if 'data_stream' not in i),
        'aliases': sorted(a['name'] for a in body.get('aliases', [])),
        'data_streams': sorted(d['name'] for d in body.get('data_streams', [])),
    })
    readable = set(result['indices']) | set(result['aliases']) | set(result['data_streams'])
    result['sources'] = [pack.summary(es.settings.tool_prefix) for pack in ctx.lifespan_context.get('packs', [])
                         if any(matches_index_expression(name, pack.index) for name in readable)]
    # Clusters with daily indices can have thousands; the data sources' patterns matter more.
    result['indices'], truncated = fit_to_budget(result['indices'], es.settings.max_response_chars // 2)
    if truncated:
        result['truncated'] = True
        result['hint'] = ("Only some indices are listed. Query a data source by its 'index' pattern, or use a "
                          "wildcard pattern for indices outside the data sources.")
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
            description="Fields to return for each event, e.g. ['@timestamp', 'user.name', 'source.ip'], as "
                        "a table with one column per field. Name only the fields you need; wildcards are "
                        "allowed but return whole events, which are much larger.")] = None,
        size: Annotated[int, Field(ge=1, le=MAX_SEARCH_SIZE, description=(
            f"Maximum number of events to return. From {SUMMARY_MIN_SIZE}, the result also summarises every "
            f"matching event."))] = 20,
        sort: Annotated[Literal['desc', 'asc'], Field(
            description="'desc' for most recent first, 'asc' for chronological order.")] = 'desc',
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    Search for individual events in a time range, optionally narrowed by exact-match filters and a
    full-text query. Returns the total number of matches and up to `size` events.
    To count or rank values (e.g. top source IPs), use top_values instead; don't count returned rows.
    """
    es = gateway_from(ctx)
    await check_fields(es, index, filters=filters, query=query, timestamp_field=timestamp_field)
    event_types = [p.event_type_field for p in ctx.lifespan_context.get('packs', []) if p.overlaps(index)]
    summarise = await summary_fields(es, index, event_types + (fields or []), size, timestamp_field)
    result = await run_search(
        es, index, build_query(time_range, timestamp_field, es.settings.max_time_range_days, filters, query),
        fields, size, sort, timestamp_field, summarise)
    return _add_retention_note(result, ctx, index, time_range.start)


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
    To list every value rather than the most frequent, use distinct_values.
    """
    es = gateway_from(ctx)
    await check_fields(es, index, filters=filters, query=query, aggregated=[field], timestamp_field=timestamp_field)
    result = await run_top_values(
        es, index, build_query(time_range, timestamp_field, es.settings.max_aggregation_range_days, filters, query),
        field, size, include_fields)
    return _add_retention_note(result, ctx, index, time_range.start)


async def distinct_values(
        index: Index,
        field: Annotated[str, Field(
            description="Field to list the values of. Must be keyword, numeric, ip, boolean or date, not text.")],
        time_range: TimeRange,
        ctx: Context,
        filters: Filters = [],
        query: QueryString = None,
        order: Annotated[Literal['last_seen', 'count', 'value'], Field(
            description="'last_seen' for most recently seen first, 'count' for most events first, "
                        "'value' for alphabetical.")] = 'last_seen',
        size: Annotated[int, Field(ge=1, le=1000, description="Maximum number of values to return.")] = 200,
        include_fields: Annotated[list[str] | None, Field(
            description="Fields to show from one example event per value.")] = None,
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    List every distinct value of a field over a time range, with how many events have each and when
    it was first and last seen, plus the number of distinct values. Unlike top_values, rare values
    are not left out, so use it for complete answers to questions like "which X occurred in period Y?".
    """
    es = gateway_from(ctx)
    await check_fields(es, index, filters=filters, query=query, aggregated=[field], timestamp_field=timestamp_field)
    result = await run_distinct_values(
        es, index, build_query(time_range, timestamp_field, es.settings.max_aggregation_range_days, filters, query),
        field, size, timestamp_field, order, include_fields)
    return _add_retention_note(result, ctx, index, time_range.start)


def _event_type_group(ctx: Context, index: str) -> list[str]:
    """The group_by to use when none is given: the event type field of the data source being searched."""
    fields = sorted({p.event_type_field for p in ctx.lifespan_context.get('packs', [])
                     if p.event_type_field and p.overlaps(index)})
    if len(fields) == 1:
        return fields
    if fields:
        raise ToolError(f"The data sources in '{index}' record event types in different fields "
                        f"({', '.join(fields)}); pass group_by or compare one data source at a time")
    raise ToolError(f"No known data source in '{index}' defines an event type field; pass group_by")


def _days(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400


def _classify(before: int, after: int, expected_after: float, min_change: float) -> str | None:
    if before and not after:
        return 'stopped'
    if after and not before:
        return 'new'
    if after <= expected_after * (1 - min_change):
        return 'dropped'
    if after >= expected_after * (1 + min_change):
        return 'increased'
    return None


_WANTED_CHANGES = {'down': {'stopped', 'dropped'}, 'up': {'new', 'increased'},
                   'both': {'stopped', 'dropped', 'new', 'increased'}}


async def compare_periods(
        index: Index,
        before: Annotated[TimeRange, Field(description="The baseline period.")],
        after: Annotated[TimeRange, Field(
            description="The period to compare with the baseline, e.g. from date X until now. "
                        "Must start after 'before' ends.")],
        ctx: Context,
        group_by: Annotated[list[str] | None, Field(
            min_length=1, max_length=4,
            description="Keyword, numeric, ip or boolean fields whose values (or combinations of values) "
                        "are compared, e.g. ['event.code'] or ['event.code', 'host.hostname']. Defaults to "
                        "the data source's event_type_field (see list_data_sources).")] = None,
        changes: Annotated[Literal['down', 'up', 'both'], Field(
            description="'down' for groups that stopped or dropped, 'up' for groups that are new or "
                        "increased, 'both' for all of them.")] = 'down',
        min_change_percent: Annotated[int, Field(
            ge=1, le=100, description="Smallest change in events per day to report, as a percentage of "
                                      "the baseline rate.")] = 50,
        min_events: Annotated[int, Field(
            ge=1, description="Ignore groups with fewer events than this in both periods, to skip noise "
                              "from rare values.")] = 5,
        filters: Filters = [],
        query: QueryString = None,
        size: Annotated[int, Field(ge=1, le=500, description="Maximum number of changed groups to return.")] = 50,
        timestamp_field: TimestampField = '@timestamp',
) -> dict[str, Any]:
    """
    Compare how often each value (or combination of values) of the group_by fields occurs in two
    periods, and report the groups whose rate changed: event types or hosts that stopped, dropped,
    appeared or increased since a date. Rates are per day, so the periods may differ in length, and
    each period may be as long as the maximum time range. Keep the baseline within the source's
    retention_days (see list_data_sources): before that only part of the data is kept, which skews
    the comparison.
    """
    es = gateway_from(ctx)
    group_by = group_by or _event_type_group(ctx, index)
    await check_fields(es, index, filters=filters, query=query, aggregated=group_by, timestamp_field=timestamp_field)
    max_days = es.settings.max_aggregation_range_days
    before_start, before_end = before.bounds(max_days)
    after_start, after_end = after.bounds(max_days)
    if before_end >= after_start:
        raise ToolError("The 'before' period must end before the 'after' period starts")
    before_days, after_days = _days(before_start, before_end), _days(after_start, after_end)
    if not before_days or not after_days:
        raise ToolError("Both periods must have a non-zero length")
    # Fix 'now' once, so every page of the aggregation sees the same periods.
    before_range = TimeRange(start=before_start, end=before_end).to_query(timestamp_field, max_days)
    after_range = TimeRange(start=after_start, end=after_end).to_query(timestamp_field, max_days)

    either_period = {'bool': {'should': [before_range, after_range], 'minimum_should_match': 1}}
    search_query = bool_query([either_period], filters, query)
    buckets, complete, failed_page = await _composite_buckets(
        es, index, search_query, [{f: {'terms': {'field': f}}} for f in group_by],
        {'before': {'filter': before_range}, 'after': {'filter': after_range}})
    groups = [(b['key'], b['before']['doc_count'], b['after']['doc_count']) for b in buckets]

    changed = []
    for key, before_count, after_count in groups:
        if max(before_count, after_count) < min_events:
            continue
        expected_after = before_count / before_days * after_days
        status = _classify(before_count, after_count, expected_after, min_change_percent / 100)
        if status not in _WANTED_CHANGES[changes]:
            continue
        changed.append((abs(after_count - expected_after), [
            *(key[f] for f in group_by),
            status,
            before_count,
            after_count,
            round(before_count / before_days, 2),
            round(after_count / after_days, 2),
            round((after_count / expected_after - 1) * 100) if before_count else None,
        ]))
    # Biggest difference from the baseline rate first, so a stopped busy group outranks a quiet one.
    changed.sort(key=lambda item: item[0], reverse=True)
    size, _ = _capped(es, size)
    rows, truncated = fit_to_budget([row for _, row in changed[:size]], es.settings.max_response_chars)

    before_total, after_total = sum(g[1] for g in groups), sum(g[2] for g in groups)
    result: dict[str, Any] = {
        'group_by': group_by,
        'before_days': round(before_days, 2), 'after_days': round(after_days, 2),
        'before_events': before_total, 'after_events': after_total,
        'groups_compared': len(groups), 'groups_changed': len(changed),
        'returned': len(rows),
        'columns': [*group_by, 'status', 'before_count', 'after_count', 'before_per_day', 'after_per_day',
                    'change_percent'],
        'rows': rows,
    }
    if truncated or len(rows) < len(changed):
        result['truncated'] = True
        result['hint'] = ("More groups changed than were returned; raise min_events or min_change_percent, "
                          "or add filters.")
    notes = []
    if retention := retention_note(ctx.lifespan_context.get('packs', []), index, before_start):
        notes.append(f"{retention} Groups may look new or increased only because the baseline is incomplete.")
    if not complete:
        notes.append(f"Only the first {len(groups)} groups were compared; add filters or group by fewer fields.")
    if groups and not before_total:
        notes.append("The 'before' period has no events at all; the data may not be kept that far back.")
    elif groups and not after_total:
        notes.append("The 'after' period has no events at all; ingestion may have stopped.")
    if notes:
        result['note'] = ' '.join(notes)
    return _with_shard_warning(result, failed_page)


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
    and prefer STATS over returning raw rows; queries with STATS may also cover a longer time range.
    Double quotes make a string, not a field name: write fields bare, or in backticks if they contain
    special characters, e.g. WHERE `user-agent` == "curl".
    """
    if quoted := quoted_field_names(query):
        raise ToolError(f"{', '.join(quoted)} is a string, not a field name. Write field names bare, "
                        f"or in backticks if they contain special characters, e.g. WHERE `field.name` == \"value\".")
    es = gateway_from(ctx)
    max_days = es.settings.max_aggregation_range_days if aggregates(query) else es.settings.max_time_range_days
    body = await es.esql(query, time_range.to_query(timestamp_field, max_days))

    columns = [c['name'] for c in body.get('columns', [])]
    rows = body.get('values', [])
    limited = rows[:es.settings.max_result_size]
    limited, truncated = fit_to_budget(limited, es.settings.max_response_chars)
    result: dict[str, Any] = {'columns': columns, 'returned': len(limited), 'rows': limited}
    if truncated or len(limited) < len(rows):
        result['truncated'] = True
        result['hint'] = "Output was truncated; add STATS to aggregate or a smaller LIMIT."
    elif len(rows) >= SUMMARY_MIN_SIZE and not aggregates(query):
        result['hint'] = "To count or rank these rows, run the query again with STATS rather than counting them."
    # The gateway parsed the query's indices before running it, so this can't fail here.
    return _add_retention_note(result, ctx, ','.join(source_indices(query)), time_range.start)


ALL_TOOLS = [list_data_sources, describe_fields, search_events, top_values, distinct_values, compare_periods,
             esql_query]
