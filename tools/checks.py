"""Checks the fields and Lucene query a tool call names before it runs.

Elasticsearch rejects malformed requests itself, but some mistakes are valid requests that just match
nothing, or more than intended: a field that doesn't exist, an exact match on an analysed text field,
or a lowercase 'and' that Lucene searches for as a word. These checks turn them into errors that say
how to fix them.
"""
import difflib
import re
from typing import Any, Iterable

from fastmcp.exceptions import ToolError

from tools.query import Filter
from utils.elasticsearch import ElasticsearchGateway

_ESCAPED = re.compile(r'\\.')
# Quoted phrases, ranges and regular expressions hold values, not syntax.
_LUCENE_VALUES = re.compile(r'"[^"]*"|\[[^\]]*\]|\{[^}]*\}|/[^/]*/')
_TERM_START = r'(?:^|(?<=[\s(!+]))-?'
_LUCENE_FIELD = re.compile(_TERM_START + r'([\w@][\w.@-]*)\s*:')
_EXISTS = re.compile(r'\b_exists_\s*:\s*([\w@][\w.@-]*)')
_LOWERCASE_OPERATOR = re.compile(r'(?:^|(?<=[\s(]))(and|or|not)(?=[\s(])')
_COMPARISON = re.compile(_TERM_START + r'[\w@][\w.@-]*\s*(==?|!=|[<>]=?)')

_TEXT_TYPES = {'text', 'match_only_text'}
_DATE_TYPES = {'date', 'date_nanos'}
_OBJECT_TYPES = {'object', 'nested'}
# Filter operators that compare whole values, which an analysed text field doesn't hold.
_EXACT_OPS = {'eq', 'in', 'prefix'}


def _lucene_syntax(query: str) -> str:
    return _LUCENE_VALUES.sub(' ', _ESCAPED.sub('x', query))


def check_lucene(query: str | None) -> None:
    """Reject Lucene queries that parse but don't mean what they appear to."""
    if not query:
        return
    syntax = _lucene_syntax(query)
    if operator := _LOWERCASE_OPERATOR.search(syntax):
        word = operator.group(1)
        raise ToolError(f"Lucene searches for a lowercase '{word}' as a word; write {word.upper()} for the "
                        f"operator, or quote it to search for the word.")
    if comparison := _COMPARISON.search(syntax):
        raise ToolError(f"Lucene has no '{comparison.group(1)}' operator; write field:value, "
                        f"NOT field:value or a range such as field:>=10.")


def lucene_fields(query: str | None) -> set[str]:
    """Fields a Lucene query names, other than wildcards and metadata fields."""
    if not query:
        return set()
    syntax = _lucene_syntax(query)
    return {f for f in _LUCENE_FIELD.findall(syntax) if not f.startswith('_')} | set(_EXISTS.findall(syntax))


def _subfields(name: str, mapped: dict[str, dict[str, Any]], aggregatable: bool = False) -> list[str]:
    depth = name.count('.') + 1
    return sorted(f for f, caps in mapped.items()
                  if f.startswith(name + '.') and f.count('.') == depth
                  and (not aggregatable or any(c.get('aggregatable') for c in caps.values())))


def _instead(name: str, alternatives: list[str], otherwise: str) -> str:
    return f"use {' or '.join(repr(a) for a in alternatives)}" if alternatives else otherwise


async def _suggestions(es: ElasticsearchGateway, index: str, name: str) -> list[str]:
    known = list((await es.field_caps(index, ['*'])).get('fields', {}))
    leaf = name.rsplit('.', 1)[-1].lower()
    ranked = ([f for f in known if f.lower() == name.lower()]
              + difflib.get_close_matches(name, known, n=3, cutoff=0.7)
              + sorted(f for f in known if f.lower().rsplit('.', 1)[-1] == leaf))
    return list(dict.fromkeys(ranked))[:5]


async def check_fields(es: ElasticsearchGateway, index: str, *, filters: Iterable[Filter] = (),
                       query: str | None = None, aggregated: Iterable[str] = (),
                       timestamp_field: str | None = None) -> None:
    """Check that the fields a tool call names exist in `index` and suit how they are used.

    `aggregated` fields are grouped by, so must be aggregatable; exact-match filters need a field that
    isn't analysed text; the timestamp field must be a date.
    """
    check_lucene(query)
    filters, aggregated = list(filters), set(aggregated)
    exact = {f.field for f in filters if f.op in _EXACT_OPS}
    named = {f.field for f in filters} | lucene_fields(query) | aggregated | {timestamp_field or ''}
    named = {n for n in named if n and not n.startswith('_') and not any(c in n for c in '*?')}
    if not named:
        return
    body = await es.field_caps(index, sorted({pattern for n in named for pattern in (n, f'{n}.*')}))
    mapped = body.get('fields', {})

    for name in sorted(named):
        if name not in mapped:
            hint = f"Use {es.settings.tool_prefix}describe_fields to list the fields."
            if similar := await _suggestions(es, index, name):
                hint = f"Did you mean {', '.join(similar)}? " + hint
            raise ToolError(f"No field '{name}' in '{index}'. {hint}")
        types = set(mapped[name]) - {'unmapped'}
        if types <= _OBJECT_TYPES:
            children = _subfields(name, mapped)
            raise ToolError(f"'{name}' is an object, not a field; name one of its fields"
                            + (f", e.g. {', '.join(children[:5])}." if children else "."))
        if name in exact and types <= _TEXT_TYPES:
            raise ToolError(f"'{name}' is a text field, so exact matches on it rarely work; "
                            + _instead(name, _subfields(name, mapped, aggregatable=True),
                                       "search it with the full-text query instead") + ".")
        if name in aggregated and not any(c.get('aggregatable') for c in mapped[name].values()):
            raise ToolError(f"'{name}' can't be grouped by; "
                            + _instead(name, _subfields(name, mapped, aggregatable=True),
                                       "choose a keyword, numeric, ip, boolean or date field") + ".")
        if name == timestamp_field and not types & _DATE_TYPES:
            raise ToolError(f"Timestamp field '{name}' is not a date field; pass a date field as timestamp_field.")
