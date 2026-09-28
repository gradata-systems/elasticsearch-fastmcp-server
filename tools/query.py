"""Query building blocks shared by the generic tools: time ranges, filters and output budgets."""
import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

_DATE_ONLY = re.compile(r'^\d{4}-\d{2}-\d{2}$')

Scalar = str | int | float | bool


class TimeRange(BaseModel):
    start: datetime = Field(
        description="Start of the period, inclusive, ISO 8601: a date ('2026-09-01') or a UTC datetime "
                    "('2026-09-01T08:00:00Z').")
    end: datetime | None = Field(
        default=None,
        description="End of the period, inclusive, ISO 8601. A date alone covers that whole day. Defaults to now.")

    @field_validator('start', 'end', mode='before')
    @classmethod
    def _end_date_covers_whole_day(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name == 'end' and isinstance(value, str) and _DATE_ONLY.match(value):
            return datetime.combine(datetime.fromisoformat(value).date(), time(23, 59, 59, 999000))
        return value

    @field_validator('start', 'end')
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode='after')
    def _ordered(self) -> 'TimeRange':
        if self.end is not None and self.end < self.start:
            raise ValueError("end must not be before start")
        return self

    def to_query(self, timestamp_field: str, max_days: int) -> dict[str, Any]:
        end = self.end or datetime.now(timezone.utc)
        if end - self.start > timedelta(days=max_days):
            raise ToolError(f"Time range exceeds the maximum of {max_days} days; narrow it or split the query")
        return {'range': {timestamp_field: {
            'gte': self.start.isoformat(timespec='milliseconds'),
            'lte': end.isoformat(timespec='milliseconds'),
        }}}


class Filter(BaseModel):
    field: str = Field(description="Field name, e.g. 'user.name' or 'source.ip'. Use describe_fields to find fields.")
    op: Literal['eq', 'in', 'exists', 'prefix', 'gt', 'gte', 'lt', 'lte'] = Field(
        default='eq',
        description="'eq' and 'in' match exact values, so use them on keyword, numeric, ip, boolean and date "
                    "fields (not text fields; use the full-text query for those). 'in' takes a list. "
                    "'exists' takes no value.")
    value: Scalar | list[Scalar] | None = None
    negate: bool = Field(default=False, description="Exclude matching events instead of requiring them.")

    @model_validator(mode='after')
    def _value_matches_op(self) -> 'Filter':
        if self.op == 'exists':
            if self.value is not None:
                raise ValueError("'exists' takes no value")
        elif self.op == 'in':
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("'in' requires a non-empty list value")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"'{self.op}' requires a single value")
        return self

    def to_query(self) -> dict[str, Any]:
        match self.op:
            case 'eq':
                return {'term': {self.field: self.value}}
            case 'in':
                return {'terms': {self.field: self.value}}
            case 'exists':
                return {'exists': {'field': self.field}}
            case 'prefix':
                return {'prefix': {self.field: str(self.value)}}
            case _:
                return {'range': {self.field: {self.op: self.value}}}


def build_query(time_range: TimeRange, timestamp_field: str, max_days: int,
                filters: list[Filter], query: str | None) -> dict[str, Any]:
    must = [time_range.to_query(timestamp_field, max_days)]
    must += [f.to_query() for f in filters if not f.negate]
    bool_query: dict[str, Any] = {'filter': must}
    if must_not := [f.to_query() for f in filters if f.negate]:
        bool_query['must_not'] = must_not
    if query:
        bool_query['must'] = [{'query_string': {'query': query, 'allow_leading_wildcard': False}}]
    return {'bool': bool_query}


def fit_to_budget(rows: list[Any], max_chars: int) -> tuple[list[Any], bool]:
    """Keep leading rows whose combined JSON size fits within `max_chars`."""
    kept, used = [], 0
    for row in rows:
        used += len(json.dumps(row, default=str))
        if used > max_chars:
            return kept, True
        kept.append(row)
    return kept, False
