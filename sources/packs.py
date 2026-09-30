"""Source packs: YAML descriptions of a data source and the curated tools built on it.

A pack names an index, explains its key fields for the model, and declares tools as a search,
top-values or distinct-values query with fixed filters plus a few named parameters. Adding a data
source is a new YAML file, not new code; the generic tools remain available for anything a pack
doesn't cover.
"""
import fnmatch
import logging
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool, ToolResult
from pydantic import (BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator,
                      model_validator)
from pydantic.json_schema import SkipJsonSchema

from security.policy import matches_index_expression
from tools.generic import (MAX_SEARCH_SIZE, retention_note, run_distinct_values, run_search, run_top_values,
                           summary_fields)
from tools.query import Filter, TimeRange, build_query
from utils.elasticsearch import gateway_from

logger = logging.getLogger(__name__)

_NAME = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
# Characters Elasticsearch forbids in index names, other than the '*' wildcard.
_INVALID_INDEX_CHARS = re.compile(r'[\s\\/?"<>|#]')


# Most results a caller may ask a generated tool for, by kind; the generic tools have the same caps.
_SIZE_LIMITS = {'search': MAX_SEARCH_SIZE, 'top_values': 100, 'distinct_values': 1000}


class ParamSpec(BaseModel):
    """A tool parameter matched exactly against one or more fields (any of them may match)."""

    model_config = ConfigDict(extra='forbid')

    description: str
    fields: list[str] = Field(min_length=1)
    type: Literal['string', 'integer'] = 'string'
    match: Literal['eq', 'prefix'] = 'eq'
    required: bool = True

    @model_validator(mode='after')
    def _prefix_needs_string(self) -> 'ParamSpec':
        if self.match == 'prefix' and self.type != 'string':
            raise ValueError("prefix matching requires a string parameter")
        return self

    def to_query(self, value: Any) -> dict[str, Any]:
        clauses = [{'term' if self.match == 'eq' else 'prefix': {f: value}} for f in self.fields]
        if len(clauses) == 1:
            return clauses[0]
        return {'bool': {'should': clauses, 'minimum_should_match': 1}}


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    kind: Literal['search', 'top_values', 'distinct_values']
    description: str
    params: dict[str, ParamSpec] = {}
    filters: list[Filter] = []
    query: str | None = Field(default=None, description="Fixed Lucene query, combined with the filters.")
    size: int = Field(default=20, ge=1, le=500, description="Default result size; callers may lower or raise it.")
    # search
    fields: list[str] | None = Field(default=None, description="Fields to return; defaults to the pack's.")
    sort: Literal['asc', 'desc'] = 'desc'
    # top_values and distinct_values
    field: str | None = None
    include_fields: list[str] = []
    # distinct_values
    order: Literal['last_seen', 'count', 'value'] = 'last_seen'

    @model_validator(mode='after')
    def _check(self) -> 'ToolSpec':
        if not _NAME.match(self.name):
            raise ValueError(f"tool name '{self.name}' must be lower_snake_case")
        if self.kind != 'search' and not self.field:
            raise ValueError(f"{self.kind} tool '{self.name}' needs 'field'")
        if self.size > _SIZE_LIMITS[self.kind]:
            raise ValueError(f"{self.kind} tool '{self.name}' size must be at most {_SIZE_LIMITS[self.kind]}")
        reserved = {'time_range', 'size'} & set(self.params)
        if reserved or not all(_NAME.match(p) for p in self.params):
            raise ValueError(f"invalid parameter names in '{self.name}': {sorted(self.params)}")
        return self


class SourcePack(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    title: str
    description: str
    index: str
    retention_days: int | None = Field(
        default=None, ge=1,
        description="How many days back every event is kept. Older periods hold only a long-term subset, or "
                    "nothing. Leave unset when nothing ages out.")
    event_type_field: str | None = Field(
        default=None, description="Field naming the kind of each event, used by compare_periods by default.")
    timestamp_field: str = '@timestamp'
    key_fields: dict[str, str] = {}
    default_fields: list[str] = []
    tools: list[ToolSpec] = []

    @field_validator('index')
    @classmethod
    def _index_expression(cls, index: str) -> str:
        """A multi-target expression: comma-separated names and wildcard patterns, with optional
        '-' exclusions after a wildcard, e.g. 'logs-*,-logs-debug-*,audit'."""
        wildcard_seen = False
        for part in index.split(','):
            target = part.removeprefix('-')
            if ':' in target or target.startswith('<'):
                raise ValueError(f"remote clusters and date math are not supported in pack indices: '{part}'")
            if not target or _INVALID_INDEX_CHARS.search(target):
                raise ValueError(f"invalid index target '{part}' in '{index}'")
            if part.startswith('-') and not wildcard_seen:
                raise ValueError(f"exclusion '{part}' in '{index}' must follow a wildcard pattern")
            wildcard_seen = wildcard_seen or '*' in part
        return index

    @classmethod
    def load(cls, path: Path) -> 'SourcePack':
        with path.open(encoding='utf-8') as f:
            return cls.model_validate(yaml.safe_load(f))

    def overlaps(self, index: str) -> bool:
        """Whether searching `index` (a multi-target expression) may read this pack's data, either
        because it names part of the pack's data or because it is a pattern broad enough to cover it."""
        includes = [part for part in self.index.split(',') if not part.startswith('-')]
        return any(matches_index_expression(target, self.index)
                   or any(fnmatch.fnmatchcase(part, target) for part in includes)
                   for target in index.split(',') if not target.startswith('-'))

    def summary(self, tool_prefix: str = '') -> dict[str, Any]:
        summary = {
            'name': self.name,
            'title': self.title,
            'description': ' '.join(self.description.split()),
            'index': self.index,
            'key_fields': self.key_fields,
            'tools': [tool_prefix + t.name for t in self.tools],
        }
        if self.retention_days:
            summary['retention_days'] = self.retention_days
        if self.event_type_field:
            summary['event_type_field'] = self.event_type_field
        return summary


def load_packs(directory: Path) -> list[SourcePack]:
    if not directory.is_dir():
        raise ValueError(f"source pack directory '{directory}' does not exist")
    packs = [SourcePack.load(path) for path in sorted(directory.glob('*.yaml'))]
    seen: dict[str, str] = {}
    for pack in packs:
        for spec in pack.tools:
            if spec.name in seen:
                raise ValueError(f"tool '{spec.name}' is defined by both '{seen[spec.name]}' and '{pack.name}'")
            seen[spec.name] = pack.name
    return packs




def _arguments_model(pack: SourcePack, spec: ToolSpec) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for name, param in spec.params.items():
        kind = int if param.type == 'integer' else str
        if param.required:
            fields[name] = (kind, Field(description=param.description))
        else:
            fields[name] = (kind | None, Field(default=None, description=param.description))
    fields['time_range'] = (TimeRange, Field(description="Period to search."))
    limit = _SIZE_LIMITS[spec.kind]
    noun = 'events' if spec.kind == 'search' else 'values'
    fields['size'] = (int, Field(default=spec.size, ge=1, le=limit, description=f"Maximum number of {noun} to return."))
    return create_model(f'{spec.name}_arguments', __config__=ConfigDict(extra='forbid'), **fields)


class SourceTool(Tool):
    """An MCP tool generated from a source pack's ToolSpec."""

    pack: SkipJsonSchema[SourcePack] = Field(exclude=True)
    spec: SkipJsonSchema[ToolSpec] = Field(exclude=True)
    arguments_model: SkipJsonSchema[type[BaseModel]] = Field(exclude=True)

    @classmethod
    def build(cls, pack: SourcePack, spec: ToolSpec) -> 'SourceTool':
        model = _arguments_model(pack, spec)
        description = f"{' '.join(spec.description.split())}\n\nData source: {pack.title} ({pack.index})."
        return cls(name=spec.name, description=description, parameters=model.model_json_schema(),
                   output_schema={'type': 'object', 'additionalProperties': True}, tags={'source', pack.name},
                   pack=pack, spec=spec, arguments_model=model)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            args = self.arguments_model.model_validate(arguments)
        except ValidationError as e:
            raise ToolError(f"Invalid arguments: {e}") from e
        es = gateway_from(get_context())
        spec, pack = self.spec, self.pack

        extra = [spec.params[name].to_query(getattr(args, name))
                 for name in spec.params if getattr(args, name) is not None]
        # Tools that return counts may cover a longer period than those returning events.
        max_days = es.settings.max_time_range_days if spec.kind == 'search' else es.settings.max_aggregation_range_days
        query = build_query(args.time_range, pack.timestamp_field, max_days, spec.filters, spec.query, extra)
        if spec.kind == 'search':
            fields = spec.fields or pack.default_fields or None
            summarise = await summary_fields(es, pack.index, [pack.event_type_field, *(fields or [])], args.size,
                                             pack.timestamp_field)
            result = await run_search(es, pack.index, query, fields, args.size, spec.sort, pack.timestamp_field,
                                      summarise)
        elif spec.kind == 'top_values':
            result = await run_top_values(es, pack.index, query, spec.field, args.size, spec.include_fields)
        else:
            result = await run_distinct_values(es, pack.index, query, spec.field, args.size, pack.timestamp_field,
                                               spec.order, spec.include_fields)
        if note := retention_note([pack], pack.index, args.time_range.start):
            result['note'] = note
        return self.convert_result(result)


def exposed_packs(packs: list[SourcePack], may_serve) -> list[SourcePack]:
    """Packs whose index this server exposes; others are skipped with a warning."""
    kept = []
    for pack in packs:
        if may_serve(pack.index):
            kept.append(pack)
        else:
            logger.warning("Source pack '%s' skipped: index '%s' is not in exposed_indices", pack.name, pack.index)
    return kept
