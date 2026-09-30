"""Exports: event searches too large for the model, handed to the user as a resource link.

export_events counts the matching events and returns a link rather than the events. Reading the link
runs the search again, as whoever reads it, and returns the events as CSV (when the fields are named
exactly) or NDJSON, up to ES_MCP_MAX_EXPORT_ROWS. The link holds the search itself, with its time
range fixed, so nothing is stored on the server and any replica can serve it. Anyone who has the link
can read only what their own Elasticsearch permissions allow.
"""
import base64
import csv
import io
import json
import re
import zlib
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import mcp.types as mt
from fastmcp import Context
from fastmcp.exceptions import ResourceError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.tools import ToolResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.checks import check_fields
from tools.generic import Filters, Index, QueryString, TimestampField, retention_note
from tools.query import Filter, TimeRange, build_query, field_value, flatten
from utils.elasticsearch import ElasticsearchGateway, gateway_from, incomplete

URI_TEMPLATE = 'elasticsearch://export/{spec}/{filename}'
# Longest decoded search a link may hold, so a crafted link can't make the server inflate a huge one.
_MAX_SPEC_BYTES = 64 * 1024


class ExportSpec(BaseModel):
    """The search an export link runs."""

    model_config = ConfigDict(extra='forbid')

    index: str
    time_range: TimeRange
    filters: list[Filter] = []
    query: str | None = None
    fields: list[str] | None = None
    sort: Literal['desc', 'asc'] = 'desc'
    timestamp_field: str = '@timestamp'

    @property
    def csv(self) -> bool:
        return bool(self.fields) and not any('*' in f or '?' in f for f in self.fields)

    @property
    def mime_type(self) -> str:
        return 'text/csv' if self.csv else 'application/x-ndjson'

    def encode(self) -> str:
        data = self.model_dump_json(exclude_defaults=True).encode()
        return base64.urlsafe_b64encode(zlib.compress(data, 9)).decode().rstrip('=')

    @classmethod
    def decode(cls, spec: str) -> 'ExportSpec':
        try:
            inflater = zlib.decompressobj()
            data = inflater.decompress(base64.urlsafe_b64decode(spec + '=' * (-len(spec) % 4)), _MAX_SPEC_BYTES)
            if inflater.unconsumed_tail or not inflater.eof:
                raise ValueError("too long or cut short")
            return cls.model_validate_json(data)
        except (ValueError, zlib.error, ValidationError) as e:
            raise ResourceError("This is not a valid export link; ask for the export again") from e

    def uri(self) -> str:
        start, end = self.time_range.start, self.time_range.end
        name = re.sub(r'[^\w.-]+', '_', self.index).strip('_.-')[:40] or 'events'
        filename = f"{name}-{start:%Y%m%d}-{end:%Y%m%d}.{'csv' if self.csv else 'ndjson'}"
        return URI_TEMPLATE.format(spec=self.encode(), filename=filename)


async def export_events(
        index: Index,
        time_range: TimeRange,
        ctx: Context,
        filters: Filters = [],
        query: QueryString = None,
        fields: Annotated[list[str] | None, Field(
            description="Fields to export, e.g. ['@timestamp', 'user.name', 'source.ip']. Named exactly, the "
                        "export is CSV with one column per field; left out or with wildcards, it is NDJSON "
                        "with whole events.")] = None,
        sort: Annotated[Literal['desc', 'asc'], Field(
            description="'desc' for most recent first, 'asc' for chronological order.")] = 'desc',
        timestamp_field: TimestampField = '@timestamp',
) -> ToolResult:
    """
    Export the events matching a search to a file the user can open or save, when they want the
    events themselves rather than an answer: more rows than a search returns, or data to work with
    elsewhere. Returns how many events the export holds and a link to it; the events are not shown
    to you. Tell the user the export is ready, and don't try to read it yourself. Only clients that
    show resource links (such as VS Code) can open it.
    """
    es = gateway_from(ctx)
    await check_fields(es, index, filters=filters, query=query, timestamp_field=timestamp_field)
    # Fix the period now, so the link returns the same events whenever it is read.
    start, end = time_range.bounds(es.settings.max_time_range_days)
    spec = ExportSpec(index=index, time_range=TimeRange(start=start, end=end), filters=filters, query=query,
                      fields=fields, sort=sort, timestamp_field=timestamp_field)
    body = await es.search(index, size=0, track_total_hits=True,
                           query=build_query(spec.time_range, timestamp_field, es.settings.max_time_range_days,
                                             filters, query))
    total = body['hits']['total']['value']
    rows = min(total, es.settings.max_export_rows)
    uri = spec.uri()

    notes = ["The user can open or save the export from the link in this result. Its events are not shown "
             "to you; don't read it yourself."]
    if retention := retention_note(ctx.lifespan_context.get('packs', []), index, start):
        notes.append(retention)
    result: dict[str, Any] = {'total': total, 'exported': rows, 'format': 'csv' if spec.csv else 'ndjson',
                              'uri': uri, 'note': ' '.join(notes)}
    if rows < total:
        result['hint'] = (f"Only the {'most recent' if sort == 'desc' else 'earliest'} {rows} of {total} events "
                          f"are exported; add filters or narrow the time range to export them all.")
    if reason := incomplete(body):
        result['warning'] = f"The count is incomplete because part of the data could not be searched: {reason}"
    link = mt.ResourceLink(type='resource_link', uri=uri, name=uri.rsplit('/', 1)[1], mime_type=spec.mime_type,
                           description=f"{rows} events from {index}, {start:%Y-%m-%d %H:%M} to "
                                       f"{end:%Y-%m-%d %H:%M} UTC")
    return ToolResult(content=[mt.TextContent(type='text', text=json.dumps(result)), link],
                      structured_content=result)


def _cell(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return str(value)


def render(spec: ExportSpec, sources: list[dict[str, Any]]) -> str:
    """The events as CSV with a header row, or as NDJSON with one flattened event per line."""
    if spec.csv:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\n')
        writer.writerow(spec.fields)
        writer.writerows([_cell(field_value(s, f)) for f in spec.fields] for s in sources)
        return out.getvalue()
    return ''.join(json.dumps(flatten(s), default=str) + '\n' for s in sources)


async def run_export(es: ElasticsearchGateway, spec: ExportSpec) -> ResourceResult:
    params: dict[str, Any] = {
        'query': build_query(spec.time_range, spec.timestamp_field, es.settings.max_time_range_days, spec.filters,
                             spec.query),
        'size': es.settings.max_export_rows,
        'sort': [{spec.timestamp_field: {'order': spec.sort, 'unmapped_type': 'date'}}],
        'track_total_hits': True,
    }
    if spec.fields:
        params['_source'] = spec.fields
    body = await es.search(spec.index, limit=es.settings.max_export_rows, **params)
    sources = [h.get('_source', {}) for h in body['hits']['hits']]
    meta = {'total': body['hits']['total']['value'], 'exported': len(sources),
            'exported_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    if reason := incomplete(body):
        meta['warning'] = f"Part of the data could not be searched: {reason}"
    return ResourceResult([ResourceContent(render(spec, sources), mime_type=spec.mime_type, meta=meta)])


async def read_export(spec: str, filename: str, ctx: Context) -> ResourceResult:
    """Events exported by export_events. Open the link that tool returns; these links are not written by hand."""
    return await run_export(gateway_from(ctx), ExportSpec.decode(spec))


ALL_TOOLS = [export_events]
